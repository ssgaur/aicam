package ai.camera.shail

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class UploadNetworkPolicyTest {
    @Test
    fun allowsValidatedWifiWithInternet() {
        assertTrue(UploadNetworkPolicy.allowsUpload(true, true, true))
    }

    @Test
    fun rejectsValidatedCellularTransport() {
        assertFalse(UploadNetworkPolicy.allowsUpload(true, true, false))
    }

    @Test
    fun rejectsWifiUntilAndroidValidatesInternetAccess() {
        assertFalse(UploadNetworkPolicy.allowsUpload(true, false, true))
        assertFalse(UploadNetworkPolicy.allowsUpload(false, true, true))
    }
}
