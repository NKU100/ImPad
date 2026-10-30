package io.github.nku100.impad.runtime

import io.github.nku100.impad.rules.SemanticRuleCatalog
import java.security.MessageDigest
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.put

object SemanticPlanCacheKey {
    fun create(build: InstalledBuild, catalog: SemanticRuleCatalog, inputFingerprint: String, executionIdentitySha256: String): String {
        catalog.policy.validateIdentity(build.identity)
        require(build.apkFileSha256.size == build.apkFiles.size && build.apkFileSha256.isNotEmpty())
        require((build.apkFileSha256 + inputFingerprint + executionIdentitySha256).all { it.matches(SHA256) })
        require(build.identity.apkSha256?.equals(build.apkFileSha256.first(), ignoreCase = true) == true) {
            "Verified base APK identity does not match the installed input hashes"
        }
        val document = buildJsonObject {
            put("protocol", "SEMANTIC_STARTUP_PLAN_CACHE_V1")
            put("appId", catalog.appId)
            put("packageName", build.identity.packageName)
            put("versionName", build.identity.versionName)
            put("versionCode", build.identity.versionCode)
            put("abi", build.identity.abi)
            put("signerSha256", build.identity.signerSha256.lowercase())
            put("installedApkSha256", buildJsonArray { build.apkFileSha256.forEach { add(it.lowercase()) } })
            put("inputFingerprint", inputFingerprint.lowercase())
            put("catalogSha256", catalog.sha256)
            put("executionIdentitySha256", executionIdentitySha256.lowercase())
        }
        return MessageDigest.getInstance("SHA-256").digest(document.toString().toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
    }
    private val SHA256 = Regex("[0-9a-fA-F]{64}")
}
