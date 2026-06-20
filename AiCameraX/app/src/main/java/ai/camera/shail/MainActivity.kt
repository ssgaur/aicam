package ai.camera.shail

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.WindowManager
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.video.FileOutputOptions
import androidx.camera.video.Quality
import androidx.camera.video.QualitySelector
import androidx.camera.video.Recorder
import androidx.camera.video.Recording
import androidx.camera.video.VideoCapture
import androidx.camera.video.VideoRecordEvent
import androidx.camera.view.PreviewView
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import ai.camera.shail.ui.theme.AICameraXTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.util.concurrent.Executor
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

private const val DEFAULT_BACKEND = "https://20.197.31.88:8100"

class MainActivity : ComponentActivity() {
    private var previewView: PreviewView? = null
    private var videoCapture: VideoCapture<Recorder>? = null
    private var recording: Recording? = null
    private var chunkJob: Job? = null
    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .writeTimeout(3, TimeUnit.MINUTES)
        .readTimeout(3, TimeUnit.MINUTES)
        .apply {
            val trustAll = object : javax.net.ssl.X509TrustManager {
                override fun checkClientTrusted(chain: Array<java.security.cert.X509Certificate>, authType: String) {}
                override fun checkServerTrusted(chain: Array<java.security.cert.X509Certificate>, authType: String) {}
                override fun getAcceptedIssuers(): Array<java.security.cert.X509Certificate> = arrayOf()
            }
            val sslContext = javax.net.ssl.SSLContext.getInstance("TLS")
            sslContext.init(null, arrayOf(trustAll), java.security.SecureRandom())
            sslSocketFactory(sslContext.socketFactory, trustAll)
            hostnameVerifier { _, _ -> true }
        }
        .build()

    private lateinit var uploadQueue: UploadQueue

    private var hasCameraPermission by mutableStateOf(false)
    private var cameraReady by mutableStateOf(false)
    private var backendUrl by mutableStateOf(DEFAULT_BACKEND)
    private var chunkSecondsText by mutableStateOf("10")
    private var sampleFpsText by mutableStateOf("2")
    private var statusText by mutableStateOf("Starting…")
    private var isRunning by mutableStateOf(false)
    private var isRecording by mutableStateOf(false)
    private var chunkIndex by mutableStateOf(0)
    private var queueStatus by mutableStateOf("↑0")
    private var backendOk by mutableStateOf<Boolean?>(null)
    private var showRetryPrompt by mutableStateOf(false)
    private var pendingOnDisk by mutableStateOf(0)

    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            hasCameraPermission = granted
            if (granted) {
                startCamera()
            } else {
                statusText = "Camera permission denied"
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        // Initialize upload queue
        uploadQueue = UploadQueue(
            context = this,
            http = http,
            getBackendUrl = { backendUrl },
            getSampleFps = { sampleFpsText.toDoubleOrNull()?.coerceIn(0.2, 10.0) ?: 2.0 },
            getChunkSeconds = { chunkSecondsText.toDoubleOrNull()?.coerceIn(3.0, 300.0) ?: 10.0 },
        )
        uploadQueue.start()

        // Check if there are unsent chunks from previous session
        val chunksDir = File(cacheDir, "aicam_chunks")
        val existingCount = chunksDir.listFiles()?.count { it.extension == "mp4" } ?: 0
        if (existingCount > 0) {
            pendingOnDisk = existingCount
            showRetryPrompt = true
            statusText = "$existingCount unsent clips found"
        }

        // Periodic UI refresh for queue stats
        lifecycleScope.launch {
            while (true) {
                queueStatus = uploadQueue.summaryText()
                delay(1000)
            }
        }

        hasCameraPermission = ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED
        setContent {
            AICameraXTheme {
                AiCameraScreen()
            }
        }
        if (hasCameraPermission) {
            startCamera()
        } else {
            permissionLauncher.launch(Manifest.permission.CAMERA)
        }
    }

    override fun onDestroy() {
        stopLoop()
        recording?.stop()
        uploadQueue.stop()
        super.onDestroy()
    }

    private fun startCamera() {
        val view = previewView ?: run {
            statusText = "Preview not ready"
            return
        }
        val providerFuture = ProcessCameraProvider.getInstance(this)
        providerFuture.addListener(
            {
                try {
                    val provider = providerFuture.get()
                    val preview = Preview.Builder().build().also {
                        it.setSurfaceProvider(view.surfaceProvider)
                    }
                    val qualitySelector = QualitySelector.fromOrderedList(
                        listOf(Quality.UHD, Quality.FHD, Quality.HD),
                        androidx.camera.video.FallbackStrategy.lowerQualityOrHigherThan(Quality.HD),
                    )
                    val recorder = Recorder.Builder()
                        .setQualitySelector(qualitySelector)
                        .build()
                    videoCapture = VideoCapture.withOutput(recorder)
                    provider.unbindAll()
                    provider.bindToLifecycle(
                        this,
                        CameraSelector.DEFAULT_BACK_CAMERA,
                        preview,
                        videoCapture,
                    )
                    cameraReady = true
                    statusText = "Camera ready"
                } catch (e: Exception) {
                    cameraReady = false
                    videoCapture = null
                    statusText = "Camera bind failed: ${e.message}"
                }
            },
            mainExecutor(),
        )
    }

    private fun mainExecutor(): Executor = ContextCompat.getMainExecutor(this)

    @Composable
    private fun AiCameraScreen() {
        Surface(modifier = Modifier.fillMaxSize(), color = Color.Black) {
            Box(modifier = Modifier.fillMaxSize()) {
                AndroidView(
                    modifier = Modifier
                        .align(Alignment.Center)
                        .fillMaxWidth()
                        .aspectRatio(16f / 9f),
                    factory = { context ->
                        PreviewView(context).apply {
                            scaleType = PreviewView.ScaleType.FIT_CENTER
                            implementationMode = PreviewView.ImplementationMode.PERFORMANCE
                            previewView = this
                            if (hasCameraPermission) startCamera()
                        }
                    },
                )
                TopStatus()
                BottomControls()
            }
        }
    }

    @Composable
    private fun TopStatus() {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(8.dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Chip("AI CameraX")
                Chip(
                    if (isRecording) "REC #$chunkIndex" else if (isRunning) "running" else if (cameraReady) "idle" else "camera loading",
                    if (isRecording) Color(0xFFB71C1C) else Color.Black,
                )
                Chip(statusText)
                Spacer(modifier = Modifier.weight(1f))
                Chip(queueStatus, Color(0xFF1B5E20))
            }
            // Retry prompt banner
            if (showRetryPrompt) {
                Spacer(modifier = Modifier.height(6.dp))
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(Color(0xFFFFF3E0), RoundedCornerShape(8.dp))
                        .padding(horizontal = 12.dp, vertical = 8.dp),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        "$pendingOnDisk unsent clips. Send now?",
                        fontSize = 12.sp,
                        color = Color(0xFF212121),
                        modifier = Modifier.weight(1f),
                    )
                    Button(
                        onClick = {
                            uploadQueue.retryAll()
                            showRetryPrompt = false
                            statusText = "Sending $pendingOnDisk queued clips..."
                        },
                        colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF2E7D32)),
                        contentPadding = ButtonDefaults.ButtonWithIconContentPadding,
                    ) {
                        Text("Send", fontSize = 11.sp)
                    }
                    Button(
                        onClick = {
                            uploadQueue.clearAll()
                            showRetryPrompt = false
                            pendingOnDisk = 0
                            statusText = "Queued clips deleted"
                        },
                        colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFC62828)),
                        contentPadding = ButtonDefaults.ButtonWithIconContentPadding,
                    ) {
                        Text("Delete", fontSize = 11.sp)
                    }
                }
            }
        }
    }

    @Composable
    private fun BoxScope.BottomControls() {
        Row(
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .fillMaxWidth()
                .background(Color.Black.copy(alpha = 0.68f), RoundedCornerShape(topStart = 12.dp, topEnd = 12.dp))
                .padding(8.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            SmallTextField(
                value = backendUrl,
                label = "Backend",
                width = 270,
                enabled = !isRunning,
                onValueChange = {
                    backendUrl = it
                    backendOk = null
                    statusText = "Backend changed"
                },
            )
            Button(
                onClick = { testBackend() },
                enabled = !isRunning,
                contentPadding = ButtonDefaults.ButtonWithIconContentPadding,
            ) {
                Text(if (backendOk == true) "✓ Test" else "Test", fontSize = 12.sp)
            }
            SmallTextField(
                value = chunkSecondsText,
                label = "Sec",
                width = 72,
                enabled = !isRunning,
                keyboardType = KeyboardType.Number,
                onValueChange = { chunkSecondsText = it.filter(Char::isDigit).take(3) },
            )
            SmallTextField(
                value = sampleFpsText,
                label = "FPS",
                width = 72,
                enabled = !isRunning,
                keyboardType = KeyboardType.Decimal,
                onValueChange = { sampleFpsText = it.take(4) },
            )
            Button(
                onClick = { if (isRunning) stopLoop() else startLoop() },
                enabled = hasCameraPermission,
                colors = ButtonDefaults.buttonColors(containerColor = if (isRunning) Color(0xFFC62828) else Color(0xFF2E7D32)),
            ) {
                Text(if (isRunning) "Stop" else "Start", fontSize = 13.sp)
            }
        }
    }

    @Composable
    private fun Chip(text: String, color: Color = Color.Black) {
        Box(
            modifier = Modifier
                .background(color.copy(alpha = 0.66f), RoundedCornerShape(999.dp))
                .padding(horizontal = 9.dp, vertical = 5.dp),
        ) {
            Text(text, color = Color.White, fontSize = 11.sp)
        }
    }

    @Composable
    private fun SmallTextField(
        value: String,
        label: String,
        width: Int,
        enabled: Boolean,
        keyboardType: KeyboardType = KeyboardType.Text,
        onValueChange: (String) -> Unit,
    ) {
        OutlinedTextField(
            modifier = Modifier
                .width(width.dp)
                .height(52.dp),
            value = value,
            onValueChange = onValueChange,
            enabled = enabled,
            singleLine = true,
            label = { Text(label, fontSize = 10.sp) },
            textStyle = MaterialTheme.typography.bodySmall.copy(color = Color.White),
            keyboardOptions = KeyboardOptions(keyboardType = keyboardType),
        )
    }

    private fun testBackend() {
        lifecycleScope.launch {
            backendOk = withContext(Dispatchers.IO) {
                try {
                    val req = Request.Builder().url("${backendUrl.trimEnd('/')}/healthz").get().build()
                    http.newCall(req).execute().use { it.isSuccessful }
                } catch (_: Exception) {
                    false
                }
            }
            statusText = if (backendOk == true) "Backend connected" else "Backend not reachable"
        }
    }

    private fun startLoop() {
        if (videoCapture == null) {
            startCamera()
            Toast.makeText(this, "Binding camera, tap Start again if needed", Toast.LENGTH_SHORT).show()
            return
        }
        isRunning = true
        chunkIndex = 0
        statusText = "Starting chunks"
        showRetryPrompt = false
        uploadQueue.resume()
        chunkJob = lifecycleScope.launch {
            while (isRunning) {
                val current = chunkIndex + 1
                chunkIndex = current
                try {
                    val seconds = chunkSecondsText.toIntOrNull()?.coerceIn(3, 300) ?: 10
                    val file = recordChunk(current, seconds)
                    // Enqueue for upload (queue handles parallel sending + retries)
                    uploadQueue.enqueue(file, current)
                    statusText = "REC #${current + 1} queued #$current"
                } catch (e: CancellationException) {
                    isRecording = false
                    statusText = "Stopped"
                    break
                } catch (e: Exception) {
                    statusText = "Chunk #$current error: ${e.message}"
                    delay(1500)
                }
                delay(100)
            }
        }
    }

    private fun stopLoop() {
        isRunning = false
        chunkJob?.cancel()
        chunkJob = null
        recording?.stop()
        recording = null
        isRecording = false
        statusText = "Stopped"
    }

    private suspend fun recordChunk(index: Int, seconds: Int): File = suspendCancellableCoroutine { cont ->
        val capture = videoCapture
        if (capture == null) {
            cont.resumeWithException(IllegalStateException("VideoCapture not ready"))
            return@suspendCancellableCoroutine
        }
        val dir = File(cacheDir, "aicam_chunks").apply { mkdirs() }
        val file = File(dir, "chunk_${System.currentTimeMillis()}_$index.mp4")
        val options = FileOutputOptions.Builder(file).build()
        val startedAt = System.currentTimeMillis()
        var finalized = false
        recording = capture.output
            .prepareRecording(this, options)
            .start(mainExecutor()) { event ->
                when (event) {
                    is VideoRecordEvent.Start -> {
                        isRecording = true
                        statusText = "Recording #$index (${seconds}s)"
                        lifecycleScope.launch {
                            val remaining = (startedAt + seconds * 1000L) - System.currentTimeMillis()
                            if (remaining > 0) delay(remaining)
                            recording?.stop()
                        }
                    }
                    is VideoRecordEvent.Finalize -> {
                        if (finalized) return@start
                        finalized = true
                        isRecording = false
                        recording = null
                        if (event.hasError()) {
                            cont.resumeWithException(IllegalStateException("CameraX finalize error ${event.error}"))
                        } else {
                            cont.resume(file)
                        }
                    }
                }
            }
        cont.invokeOnCancellation {
            recording?.stop()
            recording = null
            isRecording = false
        }
    }

}

