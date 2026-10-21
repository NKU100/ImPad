package io.github.nku100.impad.rules

import java.security.MessageDigest
import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json

@Serializable
data class SourceFileSha256(val path: String, val sha256: String)

@Serializable
data class RuleExecutionManifest(
    val schemaVersion: Int,
    val ruleSources: List<SourceFileSha256>,
    val resolverSources: List<SourceFileSha256>,
    val strategySources: List<SourceFileSha256> = emptyList(),
    val moduleSources: List<SourceFileSha256>,
    val sourceTreeSha256: String,
    val routeConfigurationSha256: String,
    val dependencyLockSha256: String,
    val dexkitLockSha256: String,
    val dexkitSourceCommit: String,
    val dexkitApiSha256: String,
    val dexkitNativeSha256: String = "",
    val dexkitPlatform: String = "",
    val dexkitAndroidApiSha256: String = "",
    val dexkitLinuxAarch64NativeSha256: String = "",
    val dexkitLinuxX8664NativeSha256: String = "",
    val dexkitAndroidArm64NativeSha256: String = "",
)

@Serializable
data class RuleExecutionIdentityReport(
    val ruleCatalogSha256: String,
    val ruleImplementationSha256: String,
    val sharedResolverImplementationSha256: String,
    val dexkitLockSha256: String,
    val dexkitSourceCommit: String,
    val dexkitApiSha256: String,
    val dexkitNativeSha256: String? = null,
    val dexkitPlatform: String? = null,
    val dexkitAndroidApiSha256: String,
    val dexkitLinuxAarch64NativeSha256: String,
    val dexkitLinuxX8664NativeSha256: String,
    val dexkitAndroidArm64NativeSha256: String,
    val strategyImplementationSha256: String,
    val cacheIdentitySha256: String,
    val sourceTreeSha256: String,
    val routeConfigurationSha256: String,
    val dependencyLockSha256: String,
    val fingerprintFileSha256: String,
)

object RuleExecutionFingerprint {
    private val json = Json { ignoreUnknownKeys = false }

    fun loadManifest(): RuleExecutionManifest = json.decodeFromString(fingerprintBytes().decodeToString())

    fun fingerprintFileSha256(): String = digest(fingerprintBytes())

    fun createReport(
        ruleCatalogSha256: String,
        manifest: RuleExecutionManifest,
        runtime: DexKitRuntimeIdentity? = null,
    ): RuleExecutionIdentityReport {
        require(manifest.schemaVersion == 1) { "Unsupported build-time module fingerprint schema" }
        runtime?.let {
            require(it.sourceCommit == manifest.dexkitSourceCommit) { "Loaded DexKit source does not match fingerprint lock" }
            require(it.apiSha256 == manifest.dexkitApiSha256) { "Loaded DexKit API does not match fingerprint lock" }
            val expectedNative = when (it.platform) {
                "linux-aarch64" -> manifest.dexkitLinuxAarch64NativeSha256
                "linux-x86_64" -> manifest.dexkitLinuxX8664NativeSha256
                else -> error("Unsupported DexKit runtime platform in fingerprint: ${it.platform}")
            }
            require(it.nativeSha256 == expectedNative) { "Loaded DexKit native does not match fingerprint lock" }
        }
        val rulesSha = digestSources(manifest.ruleSources)
        val resolverSha = digestSources(manifest.resolverSources)
        val strategySha = digestSources(manifest.strategySources)
        require(digestSources(manifest.moduleSources) == manifest.sourceTreeSha256) {
            "Generated module source fingerprint is inconsistent"
        }
        val nativeSha = runtime?.nativeSha256 ?: manifest.dexkitNativeSha256.takeIf(String::isNotEmpty)
        val platform = runtime?.platform ?: manifest.dexkitPlatform.takeIf(String::isNotEmpty)
        val cacheSha = digest(listOf(
            "catalog=$ruleCatalogSha256",
            "rules=$rulesSha",
            "resolver=$resolverSha",
            "strategy=$strategySha",
            "sourceTree=${manifest.sourceTreeSha256}",
            "route=${manifest.routeConfigurationSha256}",
            "dependencies=${manifest.dependencyLockSha256}",
            "lock=${manifest.dexkitLockSha256}",
            "source=${manifest.dexkitSourceCommit}",
            "api=${manifest.dexkitApiSha256}",
            "androidApi=${manifest.dexkitAndroidApiSha256}",
            "linuxAarch64=${manifest.dexkitLinuxAarch64NativeSha256}",
            "linuxX8664=${manifest.dexkitLinuxX8664NativeSha256}",
            "androidArm64=${manifest.dexkitAndroidArm64NativeSha256}",
            "native=${nativeSha.orEmpty()}",
            "platform=${platform.orEmpty()}",
        ).joinToString("\n", postfix = "\n"))
        return RuleExecutionIdentityReport(
            ruleCatalogSha256 = ruleCatalogSha256,
            ruleImplementationSha256 = rulesSha,
            sharedResolverImplementationSha256 = resolverSha,
            strategyImplementationSha256 = strategySha,
            dexkitLockSha256 = manifest.dexkitLockSha256,
            dexkitSourceCommit = manifest.dexkitSourceCommit,
            dexkitApiSha256 = manifest.dexkitApiSha256,
            dexkitNativeSha256 = nativeSha,
            dexkitPlatform = platform,
            dexkitAndroidApiSha256 = manifest.dexkitAndroidApiSha256,
            dexkitLinuxAarch64NativeSha256 = manifest.dexkitLinuxAarch64NativeSha256,
            dexkitLinuxX8664NativeSha256 = manifest.dexkitLinuxX8664NativeSha256,
            dexkitAndroidArm64NativeSha256 = manifest.dexkitAndroidArm64NativeSha256,
            cacheIdentitySha256 = cacheSha,
            sourceTreeSha256 = manifest.sourceTreeSha256,
            routeConfigurationSha256 = manifest.routeConfigurationSha256,
            dependencyLockSha256 = manifest.dependencyLockSha256,
            fingerprintFileSha256 = fingerprintFileSha256(),
        )
    }

    private fun fingerprintBytes(): ByteArray = RuleExecutionFingerprint::class.java
        .getResourceAsStream("/rule-execution-fingerprint.json")
        ?.use { it.readBytes() }
        ?: error("Generated rule execution fingerprint is missing")

    private fun digestSources(sources: List<SourceFileSha256>): String = digest(
        sources.sortedBy(SourceFileSha256::path).joinToString(separator = "", postfix = "") {
            "${it.path}\n${it.sha256}\n"
        },
    )

    private fun digest(value: String): String = MessageDigest.getInstance("SHA-256")
        .digest(value.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }

    private fun digest(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256")
        .digest(bytes)
        .joinToString("") { "%02x".format(it) }
}
