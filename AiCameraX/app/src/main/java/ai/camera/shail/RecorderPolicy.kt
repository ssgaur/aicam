package ai.camera.shail

data class RecorderPolicy(
    val intervalMinutes: Int,
    val durationSeconds: Int,
) {
    val intervalMillis: Long
        get() = intervalMinutes * 60_000L

    companion object {
        val INTERVAL_PRESETS = listOf(5, 10, 15)
        const val DEFAULT_INTERVAL_MINUTES = 10
        const val DEFAULT_DURATION_SECONDS = 30
        const val MIN_DURATION_SECONDS = 10
        const val MAX_DURATION_SECONDS = 60

        fun fromLocalValues(intervalMinutes: Int, durationSeconds: Int): RecorderPolicy =
            RecorderPolicy(
                intervalMinutes = intervalMinutes.takeIf { it in INTERVAL_PRESETS }
                    ?: DEFAULT_INTERVAL_MINUTES,
                durationSeconds = durationSeconds.coerceIn(
                    MIN_DURATION_SECONDS,
                    MAX_DURATION_SECONDS,
                ),
            )
    }
}

enum class RecoveryTrigger {
    BOOT,
    WATCHDOG,
}

enum class RecoveryAction {
    NONE,
    START_SERVICE,
    NOTIFY_USER,
}

object RecorderStartDecider {
    fun decide(
        enabled: Boolean,
        cameraPermissionGranted: Boolean,
        sdkInt: Int,
        trigger: RecoveryTrigger,
    ): RecoveryAction {
        if (!enabled) return RecoveryAction.NONE
        if (!cameraPermissionGranted) return RecoveryAction.NOTIFY_USER

        return when (trigger) {
            RecoveryTrigger.BOOT ->
                if (sdkInt <= 33) RecoveryAction.START_SERVICE else RecoveryAction.NOTIFY_USER
            RecoveryTrigger.WATCHDOG ->
                if (sdkInt <= 30) RecoveryAction.START_SERVICE else RecoveryAction.NOTIFY_USER
        }
    }
}
