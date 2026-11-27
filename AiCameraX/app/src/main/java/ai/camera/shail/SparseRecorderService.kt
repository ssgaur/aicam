package ai.camera.shail

import android.Manifest
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Binder
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import androidx.camera.core.CameraSelector
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.video.FallbackStrategy
import androidx.camera.video.FileOutputOptions
import androidx.camera.video.Quality
import androidx.camera.video.QualitySelector
import androidx.camera.video.Recorder
import androidx.camera.video.Recording
import androidx.camera.video.VideoCapture
import androidx.camera.video.VideoRecordEvent
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleService
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import java.io.File
import java.io.IOException
import java.util.concurrent.ExecutionException
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlin.math.ceil
import kotlin.math.min

class SparseRecorderService : LifecycleService() {
    private val binder = LocalBinder()
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val preferences by lazy { RecorderPreferences(this) }
    private lateinit var uploadQueue: UploadQueue
    private lateinit var wakeLock: PowerManager.WakeLock
    private var cameraProvider: ProcessCameraProvider? = null
    private var videoCapture: VideoCapture<Recorder>? = null
    private var previewSurfaceProvider: Preview.SurfaceProvider? = null
    private var activeRecording: Recording? = null
    private var recordingStopJob: Job? = null
    private var schedulerJob: Job? = null
    private var stateJob: Job? = null
    private var isForeground = false
    private var wakeLockRefreshAtMs = 0L

    inner class LocalBinder : Binder() {
        fun attachPreview(surfaceProvider: Preview.SurfaceProvider) {
            previewSurfaceProvider = surfaceProvider
            if (activeRecording == null) {
                serviceScope.launch {
                    runCatchingRecorderFailure { bindCamera() }
                }
            }
        }

        fun detachPreview() {
            previewSurfaceProvider = null
            if (activeRecording == null) releaseCamera()
        }

        fun retryUploads() {
            uploadQueue.retryAll()
            updateStatus("Retrying queued clips", recording = false)
        }
    }

    override fun onCreate() {
        super.onCreate()
        RecorderNotifications.createChannels(this)
        preferences.setServiceRunning(false)
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "$packageName:sparse-recorder")
            .apply { setReferenceCounted(false) }
        uploadQueue = UploadQueue(
            context = this,
            http = NetworkClientFactory.create(),
            getBackendUrl = { BuildConfig.AICAM_BACKEND_URL },
            getSampleFps = { preferences.sampleFps() },
            getChunkSeconds = { preferences.policy().durationSeconds.toDouble() },
        )
        uploadQueue.start()
        stateJob = serviceScope.launch {
            var lastQueueSummary = ""
            var lastHeartbeatAtMs = 0L
            while (isActive) {
                val queueSummary = uploadQueue.summaryText()
                if (queueSummary != lastQueueSummary) {
                    preferences.updateQueueSummary(queueSummary)
                    lastQueueSummary = queueSummary
                }
                val nowMs = System.currentTimeMillis()
                if (nowMs - lastHeartbeatAtMs >= HEARTBEAT_WRITE_INTERVAL_MS) {
                    preferences.updateHeartbeat(nowMs)
                    lastHeartbeatAtMs = nowMs
                }
                if (isForeground) {
                    if (SystemClock.elapsedRealtime() >= wakeLockRefreshAtMs) acquireWakeLock()
                    refreshNotification()
                }
                delay(5_000L)
            }
        }
    }

    override fun onBind(intent: Intent): IBinder {
        super.onBind(intent)
        return binder
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        when (intent?.action) {
            ACTION_STOP -> {
                stopRecorder()
                return Service.START_NOT_STICKY
            }
            ACTION_RETRY_UPLOADS -> {
                uploadQueue.retryAll()
                return if (preferences.isEnabled()) Service.START_STICKY else Service.START_NOT_STICKY
            }
        }

        if (intent?.action == ACTION_START) {
            preferences.setEnabled(true)
            preferences.clearError()
        } else if (!preferences.isEnabled()) {
            stopSelf()
            return Service.START_NOT_STICKY
        }

        if (!hasCameraPermission()) {
            RecorderNotifications.showRecovery(
                this,
                "Camera permission is required before unattended recording can resume.",
            )
            stopSelf()
            return Service.START_NOT_STICKY
        }

        return try {
            enterForeground()
            startScheduler()
            Service.START_STICKY
        } catch (error: SecurityException) {
            stopAfterFatalFailure(
                "Android blocked the camera foreground service: ${error.message}",
            )
            Service.START_NOT_STICKY
        } catch (error: IllegalArgumentException) {
            stopAfterFatalFailure(
                "Camera foreground service configuration failed: ${error.message}",
            )
            Service.START_NOT_STICKY
        }
    }

    private fun enterForeground() {
        preferences.updateStatus("Preparing scheduled recorder", recording = false)
        ServiceCompat.startForeground(
            this,
            RecorderNotifications.SERVICE_NOTIFICATION_ID,
            RecorderNotifications.serviceNotification(this, preferences.snapshot()),
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
            } else {
                0
            },
        )
        isForeground = true
        preferences.setServiceRunning(true)
        acquireWakeLock()
        RecorderNotifications.cancelRecovery(this)
    }

    private fun startScheduler() {
        if (schedulerJob?.isActive == true) return
        schedulerJob = serviceScope.launch {
            while (isActive && preferences.isEnabled()) {
                val policy = preferences.policy()
                val now = System.currentTimeMillis()
                val storedNext = preferences.nextClipAtMs()
                val nextClipAt = when {
                    storedNext <= 0L -> now
                    storedNext > now + policy.intervalMillis -> now
                    else -> storedNext
                }

                if (nextClipAt > now) {
                    val remainingMs = nextClipAt - now
                    val remainingMinutes = ceil(remainingMs / 60_000.0).toInt()
                    updateStatus(
                        "Next ${policy.durationSeconds}s clip in ${remainingMinutes}m",
                        recording = false,
                    )
                    delay(min(remainingMs, 30_000L))
                    continue
                }

                val sequence = preferences.nextSequence()
                val startedAtMs = System.currentTimeMillis()
                preferences.setNextClipAtMs(startedAtMs + policy.intervalMillis)
                try {
                    bindCamera()
                    val file = recordClip(
                        sequence = sequence,
                        startedAtMs = startedAtMs,
                        durationSeconds = policy.durationSeconds,
                    )
                    uploadQueue.enqueue(
                        file = file,
                        chunkIndex = sequence,
                        startedAtMs = startedAtMs,
                        durationSeconds = policy.durationSeconds,
                    )
                    preferences.setLastClipAtMs(System.currentTimeMillis())
                    updateStatus("Clip #$sequence queued", recording = false)
                } catch (cancelled: CancellationException) {
                    throw cancelled
                } catch (failure: RecorderFailure) {
                    handleRecordingFailure(failure.message ?: "Recording failed")
                } catch (error: IOException) {
                    handleRecordingFailure("Clip storage failed: ${error.message}")
                } catch (error: SecurityException) {
                    stopAfterFatalFailure("Camera access was revoked: ${error.message}")
                    return@launch
                } finally {
                    if (previewSurfaceProvider == null && activeRecording == null) {
                        releaseCamera()
                    }
                }
            }
        }
    }

    private fun handleRecordingFailure(message: String) {
        Log.e(TAG, message)
        preferences.setError(message)
        val retryAt = System.currentTimeMillis() + RECORDING_FAILURE_RETRY_MS
        preferences.setNextClipAtMs(min(preferences.nextClipAtMs(), retryAt))
        refreshNotification()
    }

    private suspend fun bindCamera() {
        if (videoCapture != null) return
        val provider = cameraProvider ?: awaitCameraProvider().also { cameraProvider = it }
        val recorder = Recorder.Builder()
            .setQualitySelector(
                QualitySelector.from(
                    Quality.HD,
                    FallbackStrategy.lowerQualityOrHigherThan(Quality.HD),
                ),
            )
            .setTargetVideoEncodingBitRate(TARGET_BITRATE)
            .build()
        val capture = VideoCapture.withOutput(recorder)
        val useCases = mutableListOf<androidx.camera.core.UseCase>(capture)
        previewSurfaceProvider?.let { surfaceProvider ->
            useCases += Preview.Builder().build().also {
                it.setSurfaceProvider(surfaceProvider)
            }
        }

        try {
            provider.unbindAll()
            provider.bindToLifecycle(
                this,
                CameraSelector.DEFAULT_BACK_CAMERA,
                *useCases.toTypedArray(),
            )
            videoCapture = capture
        } catch (error: IllegalArgumentException) {
            throw RecorderFailure("No compatible 720p back camera configuration", error)
        } catch (error: IllegalStateException) {
            throw RecorderFailure("Camera could not be bound while the app is backgrounded", error)
        }
    }

    private suspend fun awaitCameraProvider(): ProcessCameraProvider =
        suspendCancellableCoroutine { continuation ->
            val future = ProcessCameraProvider.getInstance(this)
            future.addListener(
                {
                    try {
                        continuation.resume(future.get())
                    } catch (error: ExecutionException) {
                        continuation.resumeWithException(
                            RecorderFailure("Camera provider initialization failed", error.cause ?: error),
                        )
                    } catch (error: InterruptedException) {
                        Thread.currentThread().interrupt()
                        continuation.resumeWithException(
                            RecorderFailure("Camera provider initialization was interrupted", error),
                        )
                    }
                },
                ContextCompat.getMainExecutor(this),
            )
        }

    private suspend fun recordClip(
        sequence: Int,
        startedAtMs: Long,
        durationSeconds: Int,
    ): File = suspendCancellableCoroutine { continuation ->
        val capture = videoCapture
        if (capture == null) {
            continuation.resumeWithException(RecorderFailure("Camera is not ready"))
            return@suspendCancellableCoroutine
        }
        val directory = File(filesDir, CHUNKS_DIRECTORY).apply { mkdirs() }
        val expectedBytes = durationSeconds * TARGET_BITRATE / 8L
        if (directory.usableSpace < MIN_FREE_SPACE_BYTES + expectedBytes) {
            continuation.resumeWithException(
                RecorderFailure("Recording paused: less than 150 MB of free storage remains"),
            )
            return@suspendCancellableCoroutine
        }

        val baseName = "clip_${startedAtMs}_${durationSeconds}_$sequence"
        val partialFile = File(directory, "$baseName.part")
        val finalFile = File(directory, "$baseName.mp4")
        val outputOptions = FileOutputOptions.Builder(partialFile).build()
        var finalized = false
        var localRecording: Recording? = null

        localRecording = capture.output
            .prepareRecording(this, outputOptions)
            .start(ContextCompat.getMainExecutor(this)) { event ->
                when (event) {
                    is VideoRecordEvent.Start -> {
                        activeRecording = localRecording
                        updateStatus(
                            "Recording #$sequence (${durationSeconds}s)",
                            recording = true,
                        )
                        recordingStopJob?.cancel()
                        recordingStopJob = serviceScope.launch {
                            delay(durationSeconds * 1_000L)
                            localRecording?.stop()
                        }
                    }
                    is VideoRecordEvent.Finalize -> {
                        if (finalized) return@start
                        finalized = true
                        recordingStopJob?.cancel()
                        recordingStopJob = null
                        activeRecording = null
                        if (!continuation.isActive) {
                            partialFile.delete()
                            return@start
                        }
                        if (event.hasError()) {
                            partialFile.delete()
                            continuation.resumeWithException(
                                RecorderFailure("CameraX finalize error ${event.error}"),
                            )
                        } else {
                            try {
                                if (!partialFile.renameTo(finalFile)) {
                                    partialFile.copyTo(finalFile, overwrite = true)
                                    if (!partialFile.delete()) {
                                        Log.w(TAG, "Could not remove finalized partial ${partialFile.name}")
                                    }
                                }
                                continuation.resume(finalFile)
                            } catch (error: IOException) {
                                continuation.resumeWithException(error)
                            }
                        }
                    }
                }
            }

        continuation.invokeOnCancellation {
            recordingStopJob?.cancel()
            localRecording?.stop()
        }
    }

    private suspend fun runCatchingRecorderFailure(block: suspend () -> Unit) {
        try {
            block()
        } catch (failure: RecorderFailure) {
            preferences.setError(failure.message ?: "Camera preview failed")
        } catch (error: SecurityException) {
            preferences.setError("Camera permission is unavailable: ${error.message}")
        }
    }

    private fun updateStatus(status: String, recording: Boolean) {
        preferences.updateStatus(status, recording)
        preferences.updateHeartbeat()
        if (isForeground) refreshNotification()
    }

    private fun refreshNotification() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(
            RecorderNotifications.SERVICE_NOTIFICATION_ID,
            RecorderNotifications.serviceNotification(this, preferences.snapshot()),
        )
    }

    private fun releaseCamera() {
        cameraProvider?.unbindAll()
        videoCapture = null
    }

    private fun stopRecorder() {
        preferences.setEnabled(false)
        preferences.setNextClipAtMs(0L)
        preferences.updateStatus("Stopped", recording = false)
        preferences.setServiceRunning(false)
        schedulerJob?.cancel()
        schedulerJob = null
        recordingStopJob?.cancel()
        activeRecording?.stop()
        activeRecording = null
        releaseWakeLock()
        if (previewSurfaceProvider == null) releaseCamera()
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        isForeground = false
        stopSelf()
    }

    private fun stopAfterFatalFailure(message: String) {
        RecorderNotifications.showRecovery(this, message)
        schedulerJob?.cancel()
        schedulerJob = null
        recordingStopJob?.cancel()
        activeRecording?.stop()
        activeRecording = null
        releaseWakeLock()
        releaseCamera()
        if (isForeground) {
            ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
            isForeground = false
        }
        stopSelf()
    }

    private fun acquireWakeLock() {
        if (wakeLock.isHeld) wakeLock.release()
        wakeLock.acquire(WAKE_LOCK_TIMEOUT_MS)
        wakeLockRefreshAtMs = SystemClock.elapsedRealtime() + WAKE_LOCK_REFRESH_MS
    }

    private fun releaseWakeLock() {
        if (wakeLock.isHeld) wakeLock.release()
        wakeLockRefreshAtMs = 0L
    }

    private fun hasCameraPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) ==
            PackageManager.PERMISSION_GRANTED

    override fun onDestroy() {
        schedulerJob?.cancel()
        stateJob?.cancel()
        recordingStopJob?.cancel()
        activeRecording?.stop()
        releaseWakeLock()
        releaseCamera()
        uploadQueue.stop()
        preferences.setServiceRunning(false)
        serviceScope.cancel()
        super.onDestroy()
    }

    companion object {
        const val ACTION_START = "ai.camera.shail.action.START_RECORDER"
        const val ACTION_STOP = "ai.camera.shail.action.STOP_RECORDER"
        const val ACTION_RETRY_UPLOADS = "ai.camera.shail.action.RETRY_UPLOADS"
        private const val TAG = "SparseRecorderService"
        private const val CHUNKS_DIRECTORY = "aicam_chunks"
        private const val TARGET_BITRATE = 1_000_000
        private const val MIN_FREE_SPACE_BYTES = 150L * 1024L * 1024L
        private const val RECORDING_FAILURE_RETRY_MS = 60_000L
        private const val WAKE_LOCK_TIMEOUT_MS = 20 * 60_000L
        private const val WAKE_LOCK_REFRESH_MS = 10 * 60_000L
        private const val HEARTBEAT_WRITE_INTERVAL_MS = 30_000L

        fun start(context: Context) {
            ContextCompat.startForegroundService(
                context,
                Intent(context, SparseRecorderService::class.java).setAction(ACTION_START),
            )
        }

        fun stop(context: Context) {
            context.startService(
                Intent(context, SparseRecorderService::class.java).setAction(ACTION_STOP),
            )
        }
    }
}

private class RecorderFailure(message: String, cause: Throwable? = null) : Exception(message, cause)
