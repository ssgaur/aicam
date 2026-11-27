package ai.camera.shail

object UploadNetworkPolicy {
    fun allowsUpload(
        hasInternetCapability: Boolean,
        isValidated: Boolean,
        isWifiTransport: Boolean,
    ): Boolean = hasInternetCapability && isValidated && isWifiTransport
}
