package ai.camera.shail

import org.junit.Assert.assertEquals
import org.junit.Test

class RecorderPolicyTest {
    @Test
    fun defaultsMatchSparseRecorderPolicy() {
        val policy = RecorderPolicy.fromLocalValues(
            RecorderPolicy.DEFAULT_INTERVAL_MINUTES,
            RecorderPolicy.DEFAULT_DURATION_SECONDS,
        )

        assertEquals(10, policy.intervalMinutes)
        assertEquals(30, policy.durationSeconds)
        assertEquals(600_000L, policy.intervalMillis)
    }

    @Test
    fun acceptsAllIntervalPresetsAndRejectsUnknownValues() {
        assertEquals(5, RecorderPolicy.fromLocalValues(5, 30).intervalMinutes)
        assertEquals(10, RecorderPolicy.fromLocalValues(10, 30).intervalMinutes)
        assertEquals(15, RecorderPolicy.fromLocalValues(15, 30).intervalMinutes)
        assertEquals(10, RecorderPolicy.fromLocalValues(7, 30).intervalMinutes)
    }

    @Test
    fun clampsDurationToSafeBounds() {
        assertEquals(10, RecorderPolicy.fromLocalValues(10, 1).durationSeconds)
        assertEquals(45, RecorderPolicy.fromLocalValues(10, 45).durationSeconds)
        assertEquals(60, RecorderPolicy.fromLocalValues(10, 90).durationSeconds)
    }
}
