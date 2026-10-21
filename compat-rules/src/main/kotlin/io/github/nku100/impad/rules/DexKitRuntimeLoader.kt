package io.github.nku100.impad.rules

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.luckypray.dexkit.DexKitBridge
import java.io.File
import java.security.MessageDigest

internal fun dexKitDesktopPlatform(osName: String, architecture: String): String? {
    val os = osName.lowercase()
    val arch = architecture.lowercase()
    return when {
        os.contains("linux") && arch in setOf("x86_64", "amd64") -> "linux-x86_64"
        os.contains("linux") && arch in setOf("aarch64", "arm64") -> "linux-aarch64"
        else -> null
    }
}

internal fun validatePreparedDexKit(
    directory: File,
    platform: String,
    lock: JsonObject,
    apiJar: File,
): File {
    val sourceCommit = lock["source"]?.jsonObject?.get("commit")?.jsonPrimitive?.content
        ?: error("DexKit lock has no source commit")
    val (apiKey, nativeKey) = when (platform) {
        "linux-x86_64" -> "jvmApi" to "linuxX8664"
        "linux-aarch64" -> "jvmApi" to "linuxAarch64"
        else -> error("DexKit desktop runtime is unsupported on $platform")
    }
    val apiSpec = lock["artifacts"]?.jsonObject?.get(apiKey)?.jsonObject
        ?: error("DexKit lock has no $apiKey artifact")
    val nativeSpec = lock["artifacts"]?.jsonObject?.get(nativeKey)?.jsonObject
        ?: error("DexKit lock has no $nativeKey artifact")
    require(apiSpec["sourceCommit"]?.jsonPrimitive?.content == sourceCommit) {
        "DexKit API source commit does not match the lock"
    }
    require(nativeSpec["sourceCommit"]?.jsonPrimitive?.content == sourceCommit) {
        "DexKit native source commit does not match the lock"
    }

    val manifestFile = File(directory, "dexkit-prepared.json")
    require(manifestFile.isFile) { "Missing DexKit preparation manifest: $manifestFile" }
    val manifest = Json.parseToJsonElement(manifestFile.readText()).jsonObject
    require(manifest["sourceCommit"]?.jsonPrimitive?.content == sourceCommit) {
        "DexKit manifest source commit does not match the lock"
    }
    require(manifest["platform"]?.jsonPrimitive?.content == platform) {
        "DexKit runtime platform does not match $platform"
    }

    val artifacts = manifest["artifacts"]?.jsonObject
        ?: error("DexKit manifest has no artifact hashes")
    val apiHash = apiSpec["sha256"]?.jsonPrimitive?.content
        ?: error("DexKit lock has no API hash")
    require(artifacts["dexkit.jar"]?.jsonPrimitive?.content == apiHash) {
        "DexKit API manifest hash does not match the lock"
    }
    require(apiJar.isFile && sha256(apiJar) == apiHash) {
        "DexKit API JAR does not match the lock: $apiJar"
    }

    val nativeHash = nativeSpec["sha256"]?.jsonPrimitive?.content
        ?: error("DexKit lock has no native hash")
    require(artifacts["libdexkit.so"]?.jsonPrimitive?.content == nativeHash) {
        "DexKit native manifest hash does not match the lock"
    }
    val nativeLibrary = File(directory, "libdexkit.so")
    require(nativeLibrary.isFile && sha256(nativeLibrary) == nativeHash) {
        "DexKit native library does not match the lock: $nativeLibrary"
    }
    return nativeLibrary
}

object DexKitRuntimeLoader {
    fun load(directory: File): DexKitRuntimeIdentity {
        val platform = dexKitDesktopPlatform(
            System.getProperty("os.name"),
            System.getProperty("os.arch"),
        ) ?: error("DexKit desktop runtime is unsupported on this platform")
        val lock = DexKitRuntimeLoader::class.java.getResourceAsStream("/dexkit-lock.json")
            ?.bufferedReader()?.use { Json.parseToJsonElement(it.readText()).jsonObject }
            ?: error("DexKit lock resource is missing from the checker")
        val apiJar = File(DexKitBridge::class.java.protectionDomain.codeSource.location.toURI())
        val nativeLibrary = validatePreparedDexKit(directory, platform, lock, apiJar)
        System.load(nativeLibrary.absolutePath)
        val artifacts = lock["artifacts"]?.jsonObject ?: error("DexKit lock has no artifacts")
        val nativeKey = if (platform == "linux-x86_64") "linuxX8664" else "linuxAarch64"
        val apiHash = artifacts["jvmApi"]?.jsonObject?.get("sha256")?.jsonPrimitive?.content
            ?: error("DexKit lock has no JVM API SHA-256")
        val nativeHash = artifacts[nativeKey]?.jsonObject?.get("sha256")?.jsonPrimitive?.content
            ?: error("DexKit lock has no native SHA-256 for $platform")
        val sourceCommit = lock["source"]?.jsonObject?.get("commit")?.jsonPrimitive?.content
            ?: error("DexKit lock has no source commit")
        return DexKitRuntimeIdentity(platform, sourceCommit, apiHash, nativeHash)
    }
}

data class DexKitRuntimeIdentity(
    val platform: String,
    val sourceCommit: String,
    val apiSha256: String,
    val nativeSha256: String,
)

private fun sha256(file: File): String = MessageDigest.getInstance("SHA-256")
    .digest(file.readBytes())
    .joinToString("") { "%02x".format(it) }
