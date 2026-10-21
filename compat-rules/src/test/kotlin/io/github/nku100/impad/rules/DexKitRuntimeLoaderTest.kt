package io.github.nku100.impad.rules

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import java.io.File
import java.nio.file.Files
import java.security.MessageDigest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNotNull
import kotlin.test.assertNull

class DesktopDexKitLoaderTest {
    @Test
    fun selectsTheLinuxPlatformUsedByEachRunnerArchitecture() {
        assertEquals("linux-x86_64", dexKitDesktopPlatform("Linux", "x86_64"))
        assertEquals("linux-x86_64", dexKitDesktopPlatform("Linux", "amd64"))
        assertEquals("linux-aarch64", dexKitDesktopPlatform("Linux", "aarch64"))
        assertEquals("linux-aarch64", dexKitDesktopPlatform("Linux", "arm64"))
    }

    @Test
    fun rejectsUnsupportedDesktopPlatforms() {
        assertNull(dexKitDesktopPlatform("Mac OS X", "aarch64"))
        assertNull(dexKitDesktopPlatform("Windows 11", "amd64"))
        assertNull(dexKitDesktopPlatform("Linux", "riscv64"))
    }

    @Test
    fun acceptsOnlyArtifactsWhoseManifestAndFilesMatchTheLockedSource() = withPreparedRuntime { directory, apiJar, lock ->
        val native = validatePreparedDexKit(directory, "linux-x86_64", lock, apiJar)

        assertNotNull(native)
        assertEquals("libdexkit.so", native.name)
    }

    @Test
    fun rejectsApiJarThatDiffersFromTheLock() = withPreparedRuntime { directory, apiJar, lock ->
        apiJar.writeBytes("tampered API".toByteArray())

        assertFailsWith<IllegalArgumentException> {
            validatePreparedDexKit(directory, "linux-x86_64", lock, apiJar)
        }
    }

    @Test
    fun rejectsNativeThatDiffersFromTheLock() = withPreparedRuntime { directory, _, lock ->
        File(directory, "libdexkit.so").writeBytes("tampered native".toByteArray())

        assertFailsWith<IllegalArgumentException> {
            validatePreparedDexKit(directory, "linux-x86_64", lock, File(directory, "dexkit.jar"))
        }
    }

    @Test
    fun rejectsManifestWhoseSourceCommitDiffersFromTheLock() = withPreparedRuntime { directory, apiJar, lock ->
        writeManifest(directory, sourceCommit = "b".repeat(40))

        assertFailsWith<IllegalArgumentException> {
            validatePreparedDexKit(directory, "linux-x86_64", lock, apiJar)
        }
    }

    @Test
    fun rejectsManifestApiHashThatDiffersFromTheLock() = withPreparedRuntime { directory, apiJar, lock ->
        val manifest = File(directory, "dexkit-prepared.json")
        manifest.writeText(manifest.readText().replace(sha256(API_BYTES), "0".repeat(64)))

        assertFailsWith<IllegalArgumentException> {
            validatePreparedDexKit(directory, "linux-x86_64", lock, apiJar)
        }
    }

    @Test
    fun loadsTheLockedNativeOnTheSelectedLinuxRunnerWhenRequested() {
        val runtimeDirectory = System.getenv("DEXKIT_RUNTIME_DIR") ?: return

        if (System.getProperty("os.name").contains("linux", ignoreCase = true)) {
            Class.forName("org.luckypray.dexkit.DexKitBridge")
            DexKitRuntimeLoader.load(File(runtimeDirectory))
        }
    }

    private fun withPreparedRuntime(
        block: (File, File, kotlinx.serialization.json.JsonObject) -> Unit,
    ) {
        val directory = Files.createTempDirectory("dexkit-loader-test-").toFile()
        try {
            val apiJar = File(directory, "dexkit.jar").apply { writeBytes(API_BYTES) }
            File(directory, "libdexkit.so").writeBytes(NATIVE_BYTES)
            val lock = Json.parseToJsonElement(lockJson()).jsonObject
            writeManifest(directory)
            block(directory, apiJar, lock)
        } finally {
            directory.deleteRecursively()
        }
    }

    private fun writeManifest(directory: File, sourceCommit: String = SOURCE_COMMIT) {
        File(directory, "dexkit-prepared.json").writeText(
            """
            {
              "sourceCommit": "$sourceCommit",
              "platform": "linux-x86_64",
              "artifacts": {
                "dexkit.jar": "${sha256(API_BYTES)}",
                "libdexkit.so": "${sha256(NATIVE_BYTES)}"
              }
            }
            """.trimIndent(),
        )
    }

    private fun lockJson(): String =
        """
        {
          "schemaVersion": 1,
          "source": {"commit": "$SOURCE_COMMIT"},
          "artifacts": {
            "jvmApi": {"sha256": "${sha256(API_BYTES)}", "sourceCommit": "$SOURCE_COMMIT"},
            "linuxX8664": {"sha256": "${sha256(NATIVE_BYTES)}", "sourceCommit": "$SOURCE_COMMIT"}
          }
        }
        """.trimIndent()

    private fun sha256(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256")
        .digest(bytes)
        .joinToString("") { "%02x".format(it) }

    private companion object {
        const val SOURCE_COMMIT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        val API_BYTES = "locked api".toByteArray()
        val NATIVE_BYTES = "locked native".toByteArray()
    }
}
