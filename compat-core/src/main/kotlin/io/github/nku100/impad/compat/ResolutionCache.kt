package io.github.nku100.impad.compat

import java.io.File
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.StandardCopyOption.ATOMIC_MOVE
import java.nio.file.StandardCopyOption.REPLACE_EXISTING
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json

@Serializable
data class ResolutionCacheKey(
    val apkSha256: String,
    val signerSha256: String,
    val versionCode: Long,
    val resolverVersion: Int,
    val featureRulesVersion: Int,
)

// Bump this when shared compatibility resolution behavior changes.
const val COMPATIBILITY_RESOLVER_VERSION = 4

@Serializable
data class ResolutionCacheEntry(
    val key: ResolutionCacheKey,
    val result: CompatibilityResult,
)

class ResolutionCache(private val file: File) {
    private val json = Json

    fun read(key: ResolutionCacheKey): ResolutionCacheEntry? =
        readAnyCompatible()?.takeIf { it.key == key }

    fun readAnyCompatible(): ResolutionCacheEntry? {
        if (!file.isFile) return null
        return try {
            val entry = json.decodeFromString<ResolutionCacheEntry>(file.readText(StandardCharsets.UTF_8))
            entry.takeIf { it.result.status == CompatibilityStatus.COMPATIBLE }
        } catch (_: SerializationException) {
            null
        } catch (_: IllegalArgumentException) {
            null
        } catch (_: java.io.IOException) {
            null
        }
    }

    fun write(key: ResolutionCacheKey, result: CompatibilityResult) {
        require(result.status == CompatibilityStatus.COMPATIBLE) {
            "Only compatible resolutions may be cached"
        }
        val parent = file.absoluteFile.parentFile
        Files.createDirectories(parent.toPath())
        val temporary = Files.createTempFile(parent.toPath(), "${file.name}.", ".tmp")
        try {
            val entry = ResolutionCacheEntry(key, result)
            temporary.toFile().writeText(json.encodeToString(entry), StandardCharsets.UTF_8)
            Files.move(temporary, file.toPath(), ATOMIC_MOVE, REPLACE_EXISTING)
        } finally {
            Files.deleteIfExists(temporary)
        }
    }
}
