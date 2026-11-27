package ai.camera.shail

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat

object RecorderNotifications {
    const val SERVICE_NOTIFICATION_ID = 2401
    private const val RECOVERY_NOTIFICATION_ID = 2402
    private const val SERVICE_CHANNEL_ID = "sparse_recorder"
    private const val RECOVERY_CHANNEL_ID = "recorder_recovery"

    fun createChannels(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannels(
            listOf(
                NotificationChannel(
                    SERVICE_CHANNEL_ID,
                    "Sparse camera recorder",
                    NotificationManager.IMPORTANCE_LOW,
                ).apply {
                    description = "Persistent status for unattended camera recording"
                    setShowBadge(false)
                },
                NotificationChannel(
                    RECOVERY_CHANNEL_ID,
                    "Recorder action required",
                    NotificationManager.IMPORTANCE_HIGH,
                ).apply {
                    description = "Alerts when Android requires the app to be opened to resume recording"
                },
            ),
        )
    }

    fun serviceNotification(context: Context, snapshot: RecorderSnapshot): Notification {
        val openIntent = PendingIntent.getActivity(
            context,
            10,
            Intent(context, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stopIntent = PendingIntent.getService(
            context,
            11,
            Intent(context, SparseRecorderService::class.java)
                .setAction(SparseRecorderService.ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val title = if (snapshot.recording) "AiCameraX is recording" else "AiCameraX recorder is active"

        return NotificationCompat.Builder(context, SERVICE_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle(title)
            .setContentText("${snapshot.status} · ${snapshot.queueSummary}")
            .setContentIntent(openIntent)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .addAction(0, "Stop", stopIntent)
            .build()
    }

    fun showRecovery(context: Context, message: String) {
        RecorderPreferences(context).apply {
            setServiceRunning(false)
            setError(message)
        }
        if (
            Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.POST_NOTIFICATIONS,
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            return
        }

        val openIntent = PendingIntent.getActivity(
            context,
            12,
            Intent(context, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(context, RECOVERY_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle("Open AiCameraX to resume recording")
            .setContentText(message)
            .setStyle(NotificationCompat.BigTextStyle().bigText(message))
            .setContentIntent(openIntent)
            .setAutoCancel(true)
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .addAction(0, "Open AiCameraX", openIntent)
            .build()
        NotificationManagerCompat.from(context).notify(RECOVERY_NOTIFICATION_ID, notification)
    }

    fun cancelRecovery(context: Context) {
        NotificationManagerCompat.from(context).cancel(RECOVERY_NOTIFICATION_ID)
    }
}
