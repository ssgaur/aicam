package ai.camera.shail

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.view.PreviewView
import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.rememberScrollState
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
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import ai.camera.shail.ui.theme.AICameraXTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.Request
import java.io.IOException

class MainActivity : ComponentActivity() {
    private lateinit var preferences: RecorderPreferences
    private val http by lazy { NetworkClientFactory.create() }
    private var serviceBinder: SparseRecorderService.LocalBinder? = null
    private var previewView: PreviewView? = null
    private var isServiceBound = false
    private var pendingStart = false

    private var hasCameraPermission by mutableStateOf(false)
    private var intervalMinutes by mutableStateOf(RecorderPolicy.DEFAULT_INTERVAL_MINUTES)
    private var durationText by mutableStateOf(RecorderPolicy.DEFAULT_DURATION_SECONDS.toString())
    private var sampleFpsText by mutableStateOf("0.5")
    private var backendOk by mutableStateOf<Boolean?>(null)
    private var uiMessage by mutableStateOf("Ready")
    private var snapshot by mutableStateOf(
        RecorderSnapshot(
            enabled = false,
            serviceRunning = false,
            recording = false,
            status = "Stopped",
            queueSummary = "↑0",
            nextClipAtMs = 0L,
            lastClipAtMs = 0L,
            lastError = null,
            heartbeatAtMs = 0L,
        ),
    )

    private val serviceConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName, binder: IBinder) {
            serviceBinder = binder as SparseRecorderService.LocalBinder
            isServiceBound = true
            attachPreview()
        }

        override fun onServiceDisconnected(name: ComponentName) {
            serviceBinder = null
            isServiceBound = false
        }
    }

    private val cameraPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            hasCameraPermission = granted
            if (granted) {
                attachPreview()
                if (pendingStart) continueStart()
            } else {
                pendingStart = false
                uiMessage = "Camera permission denied"
            }
        }

    private val notificationPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (!granted) {
                uiMessage = "Notifications disabled; enable them for persistent recorder status"
            }
            if (pendingStart) startServiceNow()
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        preferences = RecorderPreferences(this)
        preferences.policy().let {
            intervalMinutes = it.intervalMinutes
            durationText = it.durationSeconds.toString()
        }
        sampleFpsText = preferences.sampleFps().toString()
        snapshot = preferences.snapshot()
        hasCameraPermission = hasPermission(Manifest.permission.CAMERA)

        setContent {
            AICameraXTheme {
                AiCameraScreen()
            }
        }

        lifecycleScope.launch {
            while (true) {
                snapshot = preferences.snapshot()
                delay(1_000L)
            }
        }

        if (!hasCameraPermission) {
            cameraPermissionLauncher.launch(Manifest.permission.CAMERA)
        }
    }

    override fun onStart() {
        super.onStart()
        bindService(
            Intent(this, SparseRecorderService::class.java),
            serviceConnection,
            Context.BIND_AUTO_CREATE,
        )
    }

    override fun onStop() {
        serviceBinder?.detachPreview()
        previewView = null
        if (isServiceBound) {
            unbindService(serviceConnection)
            isServiceBound = false
            serviceBinder = null
        }
        super.onStop()
    }

    private fun attachPreview() {
        if (!hasCameraPermission) return
        val surfaceProvider = previewView?.surfaceProvider ?: return
        serviceBinder?.attachPreview(surfaceProvider)
    }

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
                            attachPreview()
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
                    when {
                        snapshot.recording -> "REC"
                        snapshot.serviceRunning -> "scheduled"
                        snapshot.enabled -> "action needed"
                        else -> "stopped"
                    },
                    if (snapshot.recording) Color(0xFFB71C1C) else Color.Black,
                )
                Chip(snapshot.status)
                if (snapshot.serviceRunning && !snapshot.recording) Chip(nextClipText())
                Spacer(modifier = Modifier.weight(1f))
                Chip(snapshot.queueSummary, Color(0xFF1B5E20))
            }
            if (snapshot.lastError != null || uiMessage != "Ready") {
                Spacer(modifier = Modifier.height(6.dp))
                Chip(snapshot.lastError ?: uiMessage, Color(0xFF8D3C00))
            }
        }
    }

    @Composable
    private fun BoxScope.BottomControls() {
        Column(
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .fillMaxWidth()
                .background(
                    Color.Black.copy(alpha = 0.72f),
                    RoundedCornerShape(topStart = 12.dp, topEnd = 12.dp),
                )
                .padding(8.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .horizontalScroll(rememberScrollState()),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Chip(BuildConfig.AICAM_BACKEND_URL)
                Button(
                    onClick = { testBackend() },
                    contentPadding = ButtonDefaults.ButtonWithIconContentPadding,
                ) {
                    Text(if (backendOk == true) "✓ Backend" else "Test backend", fontSize = 11.sp)
                }
                Text("Every", color = Color.White, fontSize = 12.sp)
                RecorderPolicy.INTERVAL_PRESETS.forEach { minutes ->
                    Button(
                        onClick = { intervalMinutes = minutes },
                        enabled = !snapshot.serviceRunning,
                        colors = ButtonDefaults.buttonColors(
                            containerColor = if (intervalMinutes == minutes) {
                                Color(0xFF1565C0)
                            } else {
                                Color(0xFF424242)
                            },
                        ),
                        contentPadding = ButtonDefaults.ButtonWithIconContentPadding,
                    ) {
                        Text("${minutes}m", fontSize = 11.sp)
                    }
                }
                SmallTextField(
                    value = durationText,
                    label = "10-60 sec",
                    width = 92,
                    enabled = !snapshot.serviceRunning,
                    keyboardType = KeyboardType.Number,
                    onValueChange = { durationText = it.filter(Char::isDigit).take(2) },
                )
                SmallTextField(
                    value = sampleFpsText,
                    label = "FPS",
                    width = 70,
                    enabled = !snapshot.serviceRunning,
                    keyboardType = KeyboardType.Decimal,
                    onValueChange = { sampleFpsText = it.take(4) },
                )
                Button(
                    onClick = { serviceBinder?.retryUploads() },
                    enabled = isServiceBound,
                    contentPadding = ButtonDefaults.ButtonWithIconContentPadding,
                ) {
                    Text("Retry queue", fontSize = 11.sp)
                }
                Button(
                    onClick = {
                        if (snapshot.serviceRunning) stopRecorder() else prepareStart()
                    },
                    colors = ButtonDefaults.buttonColors(
                        containerColor = if (snapshot.serviceRunning) {
                            Color(0xFFC62828)
                        } else {
                            Color(0xFF2E7D32)
                        },
                    ),
                ) {
                    Text(
                        when {
                            snapshot.serviceRunning -> "Stop"
                            snapshot.enabled -> "Resume"
                            else -> "Start"
                        },
                        fontSize = 13.sp,
                    )
                }
                if (snapshot.enabled && !snapshot.serviceRunning) {
                    Button(
                        onClick = { stopRecorder() },
                        colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF616161)),
                        contentPadding = ButtonDefaults.ButtonWithIconContentPadding,
                    ) {
                        Text("Disable", fontSize = 11.sp)
                    }
                }
            }
        }
    }

    @Composable
    private fun Chip(text: String, color: Color = Color.Black) {
        Box(
            modifier = Modifier
                .background(color.copy(alpha = 0.72f), RoundedCornerShape(999.dp))
                .padding(horizontal = 9.dp, vertical = 5.dp),
        ) {
            Text(text, color = Color.White, fontSize = 11.sp, maxLines = 1)
        }
    }

    @Composable
    private fun SmallTextField(
        value: String,
        label: String,
        width: Int,
        enabled: Boolean,
        keyboardType: KeyboardType,
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
            label = { Text(label, fontSize = 9.sp) },
            textStyle = MaterialTheme.typography.bodySmall.copy(color = Color.White),
            keyboardOptions = KeyboardOptions(keyboardType = keyboardType),
        )
    }

    private fun prepareStart() {
        val policy = RecorderPolicy.fromLocalValues(
            intervalMinutes = intervalMinutes,
            durationSeconds = durationText.toIntOrNull()
                ?: RecorderPolicy.DEFAULT_DURATION_SECONDS,
        )
        intervalMinutes = policy.intervalMinutes
        durationText = policy.durationSeconds.toString()
        sampleFpsText = (sampleFpsText.toDoubleOrNull() ?: 0.5)
            .coerceIn(0.2, 10.0)
            .toString()
        preferences.savePolicy(policy)
        preferences.saveSampleFps(sampleFpsText.toDouble())
        pendingStart = true

        if (!hasCameraPermission) {
            cameraPermissionLauncher.launch(Manifest.permission.CAMERA)
        } else {
            continueStart()
        }
    }

    private fun continueStart() {
        if (
            Build.VERSION.SDK_INT >= 33 &&
            !hasPermission(Manifest.permission.POST_NOTIFICATIONS)
        ) {
            notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        } else {
            startServiceNow()
        }
    }

    private fun startServiceNow() {
        pendingStart = false
        try {
            SparseRecorderService.start(this)
            uiMessage = "Recorder start requested"
        } catch (error: IllegalStateException) {
            uiMessage = "Android blocked service start: ${error.message}"
        } catch (error: SecurityException) {
            uiMessage = "Camera service permission failed: ${error.message}"
        }
    }

    private fun stopRecorder() {
        try {
            SparseRecorderService.stop(this)
            uiMessage = "Recorder stop requested"
        } catch (error: IllegalStateException) {
            uiMessage = "Recorder was already stopped"
        }
    }

    private fun testBackend() {
        lifecycleScope.launch {
            backendOk = withContext(Dispatchers.IO) {
                try {
                    val request = Request.Builder()
                        .url("${BuildConfig.AICAM_BACKEND_URL.trimEnd('/')}/healthz")
                        .get()
                        .build()
                    http.newCall(request).execute().use { it.isSuccessful }
                } catch (_: IOException) {
                    false
                } catch (_: IllegalArgumentException) {
                    false
                }
            }
            uiMessage = if (backendOk == true) "Backend connected" else "Backend not reachable"
        }
    }

    private fun nextClipText(): String {
        val remainingSeconds = ((snapshot.nextClipAtMs - System.currentTimeMillis()) / 1_000L)
            .coerceAtLeast(0L)
        return "next ${remainingSeconds / 60}m ${remainingSeconds % 60}s"
    }

    private fun hasPermission(permission: String): Boolean =
        ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED
}
