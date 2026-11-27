package ai.camera.shail

import android.content.Context

data class RecorderSnapshot(
    val enabled: Boolean,
    val serviceRunning: Boolean,
    val recording: Boolean,
    val status: String,
    val queueSummary: String,
    val nextClipAtMs: Long,
    val lastClipAtMs: Long,
    val lastError: String?,
    val heartbeatAtMs: Long,
)

class RecorderPreferences(context: Context) {
    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun policy(): RecorderPolicy = RecorderPolicy.fromLocalValues(
        intervalMinutes = prefs.getInt(
            KEY_INTERVAL_MINUTES,
            RecorderPolicy.DEFAULT_INTERVAL_MINUTES,
        ),
        durationSeconds = prefs.getInt(
            KEY_DURATION_SECONDS,
            RecorderPolicy.DEFAULT_DURATION_SECONDS,
        ),
    )

    fun savePolicy(policy: RecorderPolicy) {
        val changed = policy != policy()
        prefs.edit()
            .putInt(KEY_INTERVAL_MINUTES, policy.intervalMinutes)
            .putInt(KEY_DURATION_SECONDS, policy.durationSeconds)
            .apply {
                if (changed) putLong(KEY_NEXT_CLIP_AT, 0L)
            }
            .apply()
    }

    fun sampleFps(): Double = prefs.getString(KEY_SAMPLE_FPS, DEFAULT_SAMPLE_FPS.toString())
        ?.toDoubleOrNull()
        ?.coerceIn(MIN_SAMPLE_FPS, MAX_SAMPLE_FPS)
        ?: DEFAULT_SAMPLE_FPS

    fun saveSampleFps(value: Double) {
        prefs.edit()
            .putString(KEY_SAMPLE_FPS, value.coerceIn(MIN_SAMPLE_FPS, MAX_SAMPLE_FPS).toString())
            .apply()
    }

    fun isEnabled(): Boolean = prefs.getBoolean(KEY_ENABLED, false)

    fun setEnabled(enabled: Boolean) {
        prefs.edit().putBoolean(KEY_ENABLED, enabled).apply()
    }

    fun setServiceRunning(running: Boolean) {
        prefs.edit().putBoolean(KEY_SERVICE_RUNNING, running).apply()
    }

    fun nextClipAtMs(): Long = prefs.getLong(KEY_NEXT_CLIP_AT, 0L)

    fun setNextClipAtMs(value: Long) {
        prefs.edit().putLong(KEY_NEXT_CLIP_AT, value).apply()
    }

    fun nextSequence(): Int {
        val next = prefs.getInt(KEY_SEQUENCE, 0) + 1
        prefs.edit().putInt(KEY_SEQUENCE, next).apply()
        return next
    }

    fun setLastClipAtMs(value: Long) {
        prefs.edit().putLong(KEY_LAST_CLIP_AT, value).apply()
    }

    fun updateStatus(status: String, recording: Boolean) {
        prefs.edit()
            .putString(KEY_STATUS, status)
            .putBoolean(KEY_RECORDING, recording)
            .apply()
    }

    fun updateQueueSummary(summary: String) {
        prefs.edit().putString(KEY_QUEUE_SUMMARY, summary).apply()
    }

    fun updateHeartbeat(nowMs: Long = System.currentTimeMillis()) {
        prefs.edit().putLong(KEY_HEARTBEAT_AT, nowMs).apply()
    }

    fun setError(message: String) {
        prefs.edit()
            .putString(KEY_STATUS, message)
            .putString(KEY_LAST_ERROR, message)
            .putBoolean(KEY_RECORDING, false)
            .apply()
    }

    fun clearError() {
        prefs.edit().remove(KEY_LAST_ERROR).apply()
    }

    fun snapshot(): RecorderSnapshot = RecorderSnapshot(
        enabled = isEnabled(),
        serviceRunning = prefs.getBoolean(KEY_SERVICE_RUNNING, false),
        recording = prefs.getBoolean(KEY_RECORDING, false),
        status = prefs.getString(KEY_STATUS, "Stopped") ?: "Stopped",
        queueSummary = prefs.getString(KEY_QUEUE_SUMMARY, "↑0") ?: "↑0",
        nextClipAtMs = nextClipAtMs(),
        lastClipAtMs = prefs.getLong(KEY_LAST_CLIP_AT, 0L),
        lastError = prefs.getString(KEY_LAST_ERROR, null),
        heartbeatAtMs = prefs.getLong(KEY_HEARTBEAT_AT, 0L),
    )

    companion object {
        private const val PREFS_NAME = "aicam_recorder"
        private const val KEY_INTERVAL_MINUTES = "interval_minutes"
        private const val KEY_DURATION_SECONDS = "duration_seconds"
        private const val KEY_SAMPLE_FPS = "sample_fps"
        private const val KEY_ENABLED = "enabled"
        private const val KEY_SERVICE_RUNNING = "service_running"
        private const val KEY_NEXT_CLIP_AT = "next_clip_at"
        private const val KEY_SEQUENCE = "sequence"
        private const val KEY_LAST_CLIP_AT = "last_clip_at"
        private const val KEY_STATUS = "status"
        private const val KEY_RECORDING = "recording"
        private const val KEY_QUEUE_SUMMARY = "queue_summary"
        private const val KEY_LAST_ERROR = "last_error"
        private const val KEY_HEARTBEAT_AT = "heartbeat_at"
        private const val DEFAULT_SAMPLE_FPS = 0.5
        private const val MIN_SAMPLE_FPS = 0.2
        private const val MAX_SAMPLE_FPS = 10.0
    }
}
