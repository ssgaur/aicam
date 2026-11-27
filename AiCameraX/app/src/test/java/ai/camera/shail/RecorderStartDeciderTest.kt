package ai.camera.shail

import org.junit.Assert.assertEquals
import org.junit.Test

class RecorderStartDeciderTest {
    @Test
    fun bootStartsDirectlyThroughAndroid13() {
        assertEquals(
            RecoveryAction.START_SERVICE,
            RecorderStartDecider.decide(
                enabled = true,
                cameraPermissionGranted = true,
                sdkInt = 33,
                trigger = RecoveryTrigger.BOOT,
            ),
        )
    }

    @Test
    fun bootRequiresUserActionOnAndroid14AndLater() {
        assertEquals(
            RecoveryAction.NOTIFY_USER,
            RecorderStartDecider.decide(
                enabled = true,
                cameraPermissionGranted = true,
                sdkInt = 34,
                trigger = RecoveryTrigger.BOOT,
            ),
        )
    }

    @Test
    fun missingPermissionAlwaysRequiresUserAction() {
        assertEquals(
            RecoveryAction.NOTIFY_USER,
            RecorderStartDecider.decide(
                enabled = true,
                cameraPermissionGranted = false,
                sdkInt = 30,
                trigger = RecoveryTrigger.BOOT,
            ),
        )
    }

    @Test
    fun watchdogOnlyRestartsDirectlyBeforeAndroid12() {
        assertEquals(
            RecoveryAction.START_SERVICE,
            RecorderStartDecider.decide(true, true, 30, RecoveryTrigger.WATCHDOG),
        )
        assertEquals(
            RecoveryAction.NOTIFY_USER,
            RecorderStartDecider.decide(true, true, 31, RecoveryTrigger.WATCHDOG),
        )
    }

    @Test
    fun disabledRecorderDoesNothing() {
        assertEquals(
            RecoveryAction.NONE,
            RecorderStartDecider.decide(false, true, 36, RecoveryTrigger.BOOT),
        )
    }
}
