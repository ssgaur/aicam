package ai.camera.shail

import android.Manifest
import android.app.Application
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import androidx.core.content.ContextCompat
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import java.util.concurrent.TimeUnit

class RecorderApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        RecorderNotifications.createChannels(this)
        RecorderRecovery.scheduleWatchdog(this)
    }
}

class RecorderBootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        RecorderRecovery.recover(context, RecoveryTrigger.BOOT)
        RecorderRecovery.scheduleWatchdog(context)
    }
}

class RecorderWatchdogWorker(
    context: Context,
    params: WorkerParameters,
) : Worker(context, params) {
    override fun doWork(): Result {
        val preferences = RecorderPreferences(applicationContext)
        if (!preferences.isEnabled()) return Result.success()

        val heartbeatAge = System.currentTimeMillis() - preferences.snapshot().heartbeatAtMs
        if (heartbeatAge in 0..HEARTBEAT_STALE_MS) return Result.success()

        RecorderRecovery.recover(applicationContext, RecoveryTrigger.WATCHDOG)
        return Result.success()
    }

    companion object {
        private const val HEARTBEAT_STALE_MS = 3 * 60_000L
    }
}

object RecorderRecovery {
    private const val TAG = "RecorderRecovery"
    private const val WATCHDOG_WORK_NAME = "sparse-recorder-watchdog"

    fun scheduleWatchdog(context: Context) {
        val request = PeriodicWorkRequestBuilder<RecorderWatchdogWorker>(
            15,
            TimeUnit.MINUTES,
        ).build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            WATCHDOG_WORK_NAME,
            ExistingPeriodicWorkPolicy.KEEP,
            request,
        )
    }

    fun recover(context: Context, trigger: RecoveryTrigger) {
        val preferences = RecorderPreferences(context)
        val cameraGranted = ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.CAMERA,
        ) == PackageManager.PERMISSION_GRANTED
        when (
            RecorderStartDecider.decide(
                enabled = preferences.isEnabled(),
                cameraPermissionGranted = cameraGranted,
                sdkInt = Build.VERSION.SDK_INT,
                trigger = trigger,
            )
        ) {
            RecoveryAction.NONE -> Unit
            RecoveryAction.START_SERVICE -> startServiceOrNotify(context)
            RecoveryAction.NOTIFY_USER -> RecorderNotifications.showRecovery(
                context,
                recoveryMessage(trigger, cameraGranted),
            )
        }
    }

    private fun startServiceOrNotify(context: Context) {
        try {
            SparseRecorderService.start(context)
        } catch (error: IllegalStateException) {
            Log.w(TAG, "Background foreground-service start was rejected", error)
            RecorderNotifications.showRecovery(
                context,
                "Android blocked automatic recovery. Open AiCameraX and tap Start.",
            )
        } catch (error: SecurityException) {
            Log.e(TAG, "Camera foreground-service permission was rejected", error)
            RecorderNotifications.showRecovery(
                context,
                "Camera access must be restored in AiCameraX before recording can resume.",
            )
        }
    }

    private fun recoveryMessage(trigger: RecoveryTrigger, cameraGranted: Boolean): String {
        if (!cameraGranted) {
            return "Camera permission is missing. Open AiCameraX to grant it and restart recording."
        }
        return when (trigger) {
            RecoveryTrigger.BOOT ->
                "This Android version cannot start a camera service at boot. Open the app and tap Start."
            RecoveryTrigger.WATCHDOG ->
                "The recorder stopped responding. Open the app and tap Start to satisfy Android camera restrictions."
        }
    }
}
