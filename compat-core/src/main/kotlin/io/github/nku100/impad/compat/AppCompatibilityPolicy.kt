package io.github.nku100.impad.compat

import java.net.URI
import io.github.nku100.impad.compat.apps.wechat.WeChatCompatibilityPolicy

data class AppCompatibilityPolicy(
    val appId: String,
    val packageName: String,
    val supportedAbis: Set<String>,
    val signerSha256: String,
    val requiredHookIds: Set<String>,
    val officialApkPrefix: String,
    val targetsPath: String,
) {
    fun validateTargets(targets: List<CompatibilityTarget>) = validateTargets(targets, requireSourceUrl = true)

    fun validateLocalTargets(targets: List<CompatibilityTarget>) = validateTargets(targets, requireSourceUrl = false)

    private fun validateTargets(targets: List<CompatibilityTarget>, requireSourceUrl: Boolean) {
        require(targets.isNotEmpty()) { "Compatibility policy has no targets" }
        val identities = mutableSetOf<List<String>>()
        targets.forEach { target ->
            validateIdentity(target.identity)
            require(target.identity.versionName.matches(Regex("[0-9]+(?:\\.[0-9]+)*"))) {
                "Compatibility target version name is invalid"
            }
            require(requireNotNull(target.identity.apkSha256).matches(Regex("[a-fA-F0-9]{64}"))) {
                "Compatibility target APK SHA-256 is invalid"
            }
            require(target.hooks.map { it.id }.toSet() == requiredHookIds &&
                target.hooks.size == requiredHookIds.size && target.hooks.none { it.id.isBlank() }) {
                "Compatibility target hooks do not match the registered app policy"
            }
            require(target.hooks.all { rule ->
                rule.expectedDescriptor.isNotBlank() && when (rule.locator) {
                    HookLocator.STRING_ANCHOR -> !rule.stringAnchor.isNullOrBlank()
                    HookLocator.EXACT_DESCRIPTOR -> rule.stringAnchor == null && !rule.safeForForwardInference
                }
            }) {
                "Compatibility target hook locator is invalid"
            }
            require(identities.add(listOf(
                target.identity.packageName,
                target.identity.abi,
                target.identity.versionCode.toString(),
                requireNotNull(target.identity.apkSha256).lowercase(),
            ))) {
                "Duplicate compatibility profile identity"
            }
            if (requireSourceUrl) {
                validateSourceUrl(requireNotNull(target.sourceUrl) { "Compatibility target source URL is missing" })
            } else {
                require(target.sourceUrl == null) { "Local-only compatibility targets must not declare a source URL" }
            }
        }
    }

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
            uri.path.startsWith(prefix.path) && uri.path.removePrefix(prefix.path).matches(Regex("[A-Za-z0-9._-]+\\.apk"))) {
            "APK source URL does not match the registered app policy"
        }
    }
}

object AppCompatibilityPolicies {
    private val policies = listOf(
        WeChatCompatibilityPolicy.policy,
        io.github.nku100.impad.compat.apps.qq.QqCompatibilityPolicy.policy,
        io.github.nku100.impad.compat.apps.wecom.WeComCompatibilityPolicy.policy,
    ).associateBy(AppCompatibilityPolicy::appId)

    fun require(appId: String): AppCompatibilityPolicy =
        policies[appId] ?: throw IllegalArgumentException("Unknown or unregistered appId: $appId")
}
