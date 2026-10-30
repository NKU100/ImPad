package io.github.nku100.impad.runtime

import java.io.File
import java.io.IOException
import java.nio.charset.StandardCharsets.UTF_8
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption.ATOMIC_MOVE
import java.nio.file.StandardCopyOption.REPLACE_EXISTING
import java.security.MessageDigest
import kotlinx.serialization.SerializationException
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

class FilePlanCache(private val root: File) {
    private val json = Json { ignoreUnknownKeys = false }

    fun read(namespace: String, keySha256: String): String? {
        val key = validatedKey(namespace, keySha256)
        val file = cacheFile(namespace, key)
        if (!file.isFile) return null
        return try {
            val envelope = json.decodeFromString<Envelope>(file.readText(UTF_8))
            envelope.takeIf {
                it.schemaVersion == SCHEMA_VERSION && it.namespace == namespace && it.keySha256 == key &&
                    it.payloadSha256 == sha256(it.payload)
            }?.payload
        } catch (_: IOException) {
            null
        } catch (_: SerializationException) {
            null
        } catch (_: IllegalArgumentException) {
            null
        }
    }

    fun write(namespace: String, keySha256: String, payload: String) {
        val key = validatedKey(namespace, keySha256)
        val envelope = Envelope(SCHEMA_VERSION, namespace, key, payload, sha256(payload))
        Files.createDirectories(root.toPath())
        val destination = cacheFile(namespace, key).toPath()
        val temporary = Files.createTempFile(root.toPath(), "$namespace-$key.", ".tmp")
        try {
            temporary.toFile().writeText(json.encodeToString(envelope), UTF_8)
            try {
                Files.move(temporary, destination, ATOMIC_MOVE, REPLACE_EXISTING)
            } catch (_: AtomicMoveNotSupportedException) {
                Files.move(temporary, destination, REPLACE_EXISTING)
            }
        } catch (failure: Throwable) {
            try {
                Files.deleteIfExists(temporary)
            } catch (cleanupFailure: Throwable) {
                failure.addSuppressed(cleanupFailure)
            }
            throw failure
        }
    }

    fun invalidate(namespace: String, keySha256: String) {
        val key = validatedKey(namespace, keySha256)
        Files.deleteIfExists(cacheFile(namespace, key).toPath())
    }

    private fun validatedKey(namespace: String, keySha256: String): String {
        require(namespace.matches(NAMESPACE_PATTERN)) { "Invalid cache namespace" }
        require(keySha256.matches(SHA256_PATTERN)) { "Cache key must be a SHA-256 hex digest" }
        return keySha256.lowercase()
    }

    private fun cacheFile(namespace: String, normalizedKey: String) = File(root, "$namespace-$normalizedKey.json")

    @Serializable
    private data class Envelope(
        val schemaVersion: Int,
        val namespace: String,
        val keySha256: String,
        val payload: String,
        val payloadSha256: String,
    )

    private companion object {
        const val SCHEMA_VERSION = 1
        val NAMESPACE_PATTERN = Regex("[a-z0-9_-]+")
        val SHA256_PATTERN = Regex("[0-9a-fA-F]{64}")

        fun sha256(value: String): String = MessageDigest.getInstance("SHA-256")
            .digest(value.toByteArray(UTF_8)).joinToString("") { "%02x".format(it) }
    }
}
