package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.rules.ResolvedPlan
import java.io.File
import java.io.IOException
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.StandardCopyOption.ATOMIC_MOVE
import java.nio.file.StandardCopyOption.REPLACE_EXISTING
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json

@Serializable
data class RuleResolutionCacheKey(
    val appId: String,
    val packageName: String,
    val abi: String,
    val signerSha256: String,
    val versionCode: Long,
    val apkInputSha256: List<String>,
    val routeRuleIds: List<String>,
    val routeStrategyIds: List<String>,
    val ruleCatalogSha256: String,
    val ruleImplementationSha256: String,
    val resolverImplementationSha256: String,
    val strategyImplementationSha256: String,
    val dexkitApiSha256: String,
    val dexkitNativeSha256: String,
    val dexkitSourceCommit: String,
    val dexkitLockSha256: String,
    val executionIdentitySha256: String,
)

@Serializable
private data class CachedRulePlan(
    val schemaVersion: Int,
    val createdAtEpochMillis: Long,
    val key: RuleResolutionCacheKey,
    val plan: ResolvedPlan,
)

class RuleResolutionCache(
    private val directory: File,
    private val nowMillis: () -> Long = System::currentTimeMillis,
    private val maxAgeMillis: Long = DEFAULT_MAX_AGE_MILLIS,
) {
    private val json = Json { ignoreUnknownKeys = false }

    init {
        require(maxAgeMillis > 0) { "Rule cache lifetime must be positive" }
    }

    fun read(key: RuleResolutionCacheKey): ResolvedPlan? {
        val file = cacheFile(key.appId)
        if (!file.isFile) return null
        return try {
            json.decodeFromString<CachedRulePlan>(file.readText(StandardCharsets.UTF_8))
                .takeIf { entry ->
                    val age = nowMillis() - entry.createdAtEpochMillis
                    entry.schemaVersion == CACHE_SCHEMA_VERSION && entry.key == key && age in 0..maxAgeMillis
                }
                ?.plan
        } catch (_: SerializationException) {
            null
        } catch (_: IllegalArgumentException) {
            null
        } catch (_: IOException) {
            null
        }
    }

    fun write(key: RuleResolutionCacheKey, plan: ResolvedPlan) {
        val parent = directory.absoluteFile
        Files.createDirectories(parent.toPath())
        val file = cacheFile(key.appId)
        val temporary = Files.createTempFile(parent.toPath(), "${file.name}.", ".tmp")
        try {
            temporary.toFile().writeText(
                json.encodeToString(CachedRulePlan(CACHE_SCHEMA_VERSION, nowMillis(), key, plan)),
                StandardCharsets.UTF_8,
            )
            Files.move(temporary, file.toPath(), ATOMIC_MOVE, REPLACE_EXISTING)
        } finally {
            Files.deleteIfExists(temporary)
        }
    }

    private fun cacheFile(appId: String): File {
        require(appId.matches(Regex("[a-z][a-z0-9_-]*"))) { "Invalid rule cache appId" }
        return File(directory, "resolution-$appId.json")
    }

    private companion object {
        const val CACHE_SCHEMA_VERSION = 1
        const val DEFAULT_MAX_AGE_MILLIS = 30L * 24 * 60 * 60 * 1000
    }
}
