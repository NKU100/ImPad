package io.github.nku100.impad.compat

import java.net.URI

data class AppCompatibilityPolicy(
    val appId: String,
    val packageName: String,
    val supportedAbis: Set<String>,
    val signerSha256: String,
    val requiredHookIds: Set<String>,
    val officialApkPrefix: String,
) {
    fun validateIdentity(identity: BuildIdentity) {
        require(identity.packageName == packageName) { "APK package does not match the registered app policy" }
        require(identity.abi in supportedAbis) { "APK ABI does not match the registered app policy" }
        require(identity.signerSha256.equals(signerSha256, ignoreCase = true)) {
            "APK signer does not match the registered app policy"
        }
        require(identity.versionCode > 0) { "APK versionCode must be positive" }
        require(identity.apkSha256 == null || identity.apkSha256.matches(Regex("[a-fA-F0-9]{64}"))) {
            "APK SHA-256 is invalid"
        }
    }

    fun validateSourceUrl(sourceUrl: String) {
        val uri = runCatching { URI(sourceUrl) }.getOrNull()
        val prefix = URI(officialApkPrefix)
        require(uri != null && uri.scheme == "https" && uri.host == prefix.host && uri.port == -1 &&
            uri.rawUserInfo == null && uri.rawQuery == null && uri.rawFragment == null &&
            uri.path.startsWith(prefix.path) && uri.path.removePrefix(prefix.path)
                .matches(Regex("[A-Za-z0-9._-]+\\.apk"))) {
            "APK source URL does not match the registered app policy"
        }
    }
}

object AppCompatibilityPolicies {
    private val policies = listOf(
        io.github.nku100.impad.compat.apps.wechat.WeChatCompatibilityPolicy.policy,
        io.github.nku100.impad.compat.apps.qq.QqCompatibilityPolicy.policy,
        io.github.nku100.impad.compat.apps.wecom.WeComCompatibilityPolicy.policy,
    ).associateBy(AppCompatibilityPolicy::appId)

    fun require(appId: String): AppCompatibilityPolicy =
        policies[appId] ?: throw IllegalArgumentException("Unknown or unregistered appId: $appId")
}
