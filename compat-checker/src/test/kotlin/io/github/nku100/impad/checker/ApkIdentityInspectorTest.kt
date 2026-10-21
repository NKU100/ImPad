package io.github.nku100.impad.checker

import java.io.File
import java.nio.file.Files
import java.security.MessageDigest
import java.util.Comparator
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

class ApkIdentityInspectorTest {
    @Test
    fun readsManifestAbiSignerAndHashWithoutShellingThroughACommandString() = withFixture { sdkRoot, apk ->
        val commands = mutableListOf<List<String>>()
        val inspector = ApkIdentityInspector(sdkRoot) { command ->
            commands += command
            when {
                command.getOrNull(1) == "manifest" -> when (command.getOrNull(2)) {
                    "application-id" -> "com.tencent.mm\n"
                    "version-name" -> "8.0.90\n"
                    "version-code" -> "4000\n"
                    else -> error("Unexpected manifest query: $command")
                }

                command.getOrNull(1) == "verify" -> """V2 Signer: certificate SHA-256 digest: $SIGNER_SHA256
                    |Source Stamp Signer: certificate SHA-256 digest: $SOURCE_STAMP_SHA256
                """.trimMargin()
                else -> error("Unexpected command: $command")
            }
        }

        val identity = inspector.inspect(apk)

        assertEquals("com.tencent.mm", identity.packageName)
        assertEquals("8.0.90", identity.versionName)
        assertEquals(4000L, identity.versionCode)
        assertEquals("arm64-v8a", identity.abi)
        assertEquals(SIGNER_SHA256, identity.signerSha256)
        assertEquals(sha256(apk), identity.apkSha256)
        assertEquals(listOf("application-id", "version-name", "version-code"), commands
            .filter { it.getOrNull(1) == "manifest" }
            .map { it[2] })
        assertTrue(commands.all { it.last() == apk.absolutePath })
    }

    @Test
    fun readsAbiFromSplitApksWhileKeepingIdentityBoundToBaseApk() = withSplitFixture { sdkRoot, base, abiSplit ->
        val inspector = ApkIdentityInspector(sdkRoot) { command ->
            when {
                command.getOrNull(1) == "manifest" -> when (command.getOrNull(2)) {
                    "application-id" -> "com.tencent.wework\n"
                    "version-name" -> "5.0.9\n"
                    "version-code" -> "75141\n"
                    else -> error("Unexpected manifest query: $command")
                }

                command.getOrNull(1) == "verify" -> "V3 Signer: certificate SHA-256 digest: $SIGNER_SHA256\n"
                else -> error("Unexpected command: $command")
            }
        }

        val identity = inspector.inspect(base, listOf(abiSplit))

        assertEquals("arm64-v8a", identity.abi)
        assertEquals(sha256(base), identity.apkSha256)
        assertEquals(SIGNER_SHA256, identity.signerSha256)
    }

    @Test
    fun rejectsSplitWithDifferentPackageVersionOrSigner() = withSplitFixture { sdkRoot, base, split ->
        listOf("package", "version", "signer").forEach { mismatch ->
            val inspector = ApkIdentityInspector(sdkRoot) { command ->
                val isSplit = command.last() == split.absolutePath
                when {
                    command.getOrNull(1) == "manifest" -> when (command.getOrNull(2)) {
                        "application-id" -> if (isSplit && mismatch == "package") "com.example.foreign" else "com.tencent.wework"
                        "version-name" -> "5.0.9"
                        "version-code" -> if (isSplit && mismatch == "version") "75142" else "75141"
                        else -> error("Unexpected manifest query: $command")
                    }
                    command.getOrNull(1) == "verify" -> if (isSplit && mismatch == "signer") {
                        "V3 Signer: certificate SHA-256 digest: ${"1".repeat(64)}"
                    } else "V3 Signer: certificate SHA-256 digest: $SIGNER_SHA256"
                    else -> error("Unexpected command: $command")
                }
            }

            assertFailsWith<IllegalArgumentException> { inspector.inspect(base, listOf(split)) }
        }
    }

    private fun withFixture(block: (File, File) -> Unit) {
        val directory = Files.createTempDirectory("impad apk fixture ")
        try {
            val sdkRoot = directory.resolve("sdk")
            Files.createDirectories(sdkRoot.resolve("cmdline-tools/latest/bin"))
            Files.createDirectories(sdkRoot.resolve("build-tools/37.0.0"))
            val apk = directory.resolve("wechat sample.apk").toFile()
            ZipOutputStream(Files.newOutputStream(apk.toPath())).use { zip ->
                zip.putNextEntry(ZipEntry("lib/arm64-v8a/libfixture.so"))
                zip.write(byteArrayOf(1, 2, 3))
                zip.closeEntry()
            }
            block(sdkRoot.toFile(), apk)
        } finally {
            Files.walk(directory).use { paths ->
                paths.sorted(Comparator.reverseOrder<java.nio.file.Path>()).forEach(Files::deleteIfExists)
            }
        }
    }

    private fun withSplitFixture(block: (File, File, File) -> Unit) {
        val directory = Files.createTempDirectory("impad split apk fixture ")
        try {
            val sdkRoot = directory.resolve("sdk")
            Files.createDirectories(sdkRoot.resolve("cmdline-tools/latest/bin"))
            Files.createDirectories(sdkRoot.resolve("build-tools/37.0.0"))
            val base = directory.resolve("base.apk").toFile()
            val abiSplit = directory.resolve("config.arm64_v8a.apk").toFile()
            ZipOutputStream(Files.newOutputStream(base.toPath())).use { it.finish() }
            ZipOutputStream(Files.newOutputStream(abiSplit.toPath())).use { zip ->
                zip.putNextEntry(ZipEntry("lib/arm64-v8a/libfixture.so"))
                zip.write(byteArrayOf(1, 2, 3))
                zip.closeEntry()
            }
            block(sdkRoot.toFile(), base, abiSplit)
        } finally {
            Files.walk(directory).use { paths ->
                paths.sorted(Comparator.reverseOrder<java.nio.file.Path>()).forEach(Files::deleteIfExists)
            }
        }
    }

    private fun sha256(file: File): String = MessageDigest.getInstance("SHA-256")
        .digest(file.readBytes())
        .joinToString("") { "%02x".format(it) }

    private companion object {
        const val SIGNER_SHA256 = "0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c"
        const val SOURCE_STAMP_SHA256 = "3257d599a49d2c961a471ca9843f59d341a405884583fc087df4237b733bbd6d"
    }
}
