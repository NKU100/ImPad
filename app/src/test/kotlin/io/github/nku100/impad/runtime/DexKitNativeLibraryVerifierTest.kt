package io.github.nku100.impad.runtime

import java.io.File
import java.nio.file.Files
import java.util.zip.CRC32
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue
import org.junit.jupiter.api.Test

class DexKitNativeLibraryVerifierTest {
    @Test
    fun verifiesStoredModuleApkEntryAndLoadsItsExactArchivePath() = withDirectory { directory ->
        val libraryBytes = "locked module DexKit library".encodeToByteArray()
        val apk = zip(directory.resolve("module.apk"), listOf(ENTRY_ARM64 to libraryBytes))
        val hostLibrary = directory.resolve("host").apply { mkdirs() }
            .resolve("libdexkit.so").apply { writeBytes("untrusted host library".encodeToByteArray()) }

        val verified = DexKitNativeLibraryVerifier.verify(
            sourceApk = apk,
            nativeLibraryDir = null,
            extractNativeLibs = false,
            expectedAbi = ABI,
            expectedSha256 = sha256(libraryBytes),
        )
        var loadedPath: String? = null
        val moduleLoader = ClassLoader.getSystemClassLoader()
        DexKitNativeLibraryVerifier.load(
            verified = verified,
            moduleClassLoader = moduleLoader,
            runtimeClassLoader = moduleLoader,
            bridgeClassLoader = moduleLoader,
        ) { loadedPath = it }

        assertEquals("APK_DIRECT", verified.packagingMode)
        assertEquals("${apk.canonicalPath}!/$ENTRY_ARM64", verified.loadPath)
        assertEquals(verified.loadPath, loadedPath)
        assertFalse(loadedPath == hostLibrary.canonicalPath)
        assertTrue(verified.sourceApkPath == apk.canonicalPath)
        assertEquals(sha256(libraryBytes), verified.sha256)
    }

    @Test
    fun verifiesAndLoadsTheExactExtractedModuleLibrary() = withDirectory { directory ->
        val libraryBytes = "extracted locked DexKit library".encodeToByteArray()
        val apk = zip(directory.resolve("module.apk"), listOf(ENTRY_ARM64 to libraryBytes))
        val nativeDirectory = directory.resolve("lib/arm64").apply { mkdirs() }
        val extracted = nativeDirectory.resolve("libdexkit.so").apply { writeBytes(libraryBytes) }

        val verified = DexKitNativeLibraryVerifier.verify(
            sourceApk = apk,
            nativeLibraryDir = nativeDirectory,
            extractNativeLibs = true,
            expectedAbi = ABI,
            expectedSha256 = sha256(libraryBytes),
        )

        assertEquals("EXTRACTED_FILE", verified.packagingMode)
        assertEquals(extracted.canonicalPath, verified.loadPath)
        assertEquals(sha256(libraryBytes), verified.sha256)
    }

    @Test
    fun rejectsMissingNativeEntryWithSpecificDiagnostic() = withDirectory { directory ->
        val apk = zip(directory.resolve("module.apk"), listOf("assets/other.bin" to byteArrayOf(1)))

        val failure = assertFailsWith<IllegalStateException> {
            verifier(apk, sha256(byteArrayOf(1)))
        }

        assertTrue(failure.message.orEmpty().contains("no DexKit native entry"))
    }

    @Test
    fun rejectsWrongAbiInsteadOfUsingAnotherAbiEntry() = withDirectory { directory ->
        val bytes = byteArrayOf(1, 2, 3)
        val apk = zip(directory.resolve("module.apk"), listOf("lib/armeabi-v7a/libdexkit.so" to bytes))

        val failure = assertFailsWith<IllegalStateException> {
            verifier(apk, sha256(bytes))
        }

        assertTrue(failure.message.orEmpty().contains("uses ABI(s) armeabi-v7a; expected $ABI"))
    }

    @Test
    fun selectsTheExpectedAbiFromAMultiAbiModuleApk() = withDirectory { directory ->
        val expectedBytes = byteArrayOf(1, 2, 3)
        val otherAbiBytes = byteArrayOf(9, 8, 7, 6)
        val apk = zip(directory.resolve("module.apk"), listOf(
            ENTRY_ARM64 to expectedBytes,
            "lib/x86_64/libdexkit.so" to otherAbiBytes,
        ))

        val verified = verifier(apk, sha256(expectedBytes))

        assertEquals(ENTRY_ARM64, verified.zipEntry)
        assertEquals("${apk.canonicalPath}!/$ENTRY_ARM64", verified.loadPath)
        assertEquals(sha256(expectedBytes), verified.sha256)
    }

    @Test
    fun rejectsDuplicateExpectedAbiEntriesAsAmbiguous() = withDirectory { directory ->
        val bytes = byteArrayOf(1, 2, 3)
        val alternateName = "lib/arm64-v8a/libdexkiu.so"
        val apk = zip(directory.resolve("module.apk"), listOf(ENTRY_ARM64 to bytes, alternateName to bytes))
        replaceZipEntryName(apk, alternateName, ENTRY_ARM64)

        val failure = assertFailsWith<IllegalStateException> {
            verifier(apk, sha256(bytes))
        }

        assertTrue(failure.message.orEmpty().contains("ambiguous DexKit native entries for ABI $ABI"))
    }

    @Test
    fun rejectsApkEntryHashMismatchWithExpectedAndActualDigests() = withDirectory { directory ->
        val bytes = byteArrayOf(1, 2, 3)
        val apk = zip(directory.resolve("module.apk"), listOf(ENTRY_ARM64 to bytes))

        val failure = assertFailsWith<IllegalStateException> {
            verifier(apk, "0".repeat(64))
        }

        assertTrue(failure.message.orEmpty().contains("APK native entry SHA-256 mismatch"))
        assertTrue(failure.message.orEmpty().contains(sha256(bytes)))
    }

    @Test
    fun rejectsCompressedEntryInDirectApkMode() = withDirectory { directory ->
        val bytes = ByteArray(64) { it.toByte() }
        val apk = zip(directory.resolve("module.apk"), listOf(ENTRY_ARM64 to bytes), stored = false)

        val failure = assertFailsWith<IllegalStateException> {
            verifier(apk, sha256(bytes))
        }

        assertTrue(failure.message.orEmpty().contains("must be stored for direct APK loading"))
    }

    @Test
    fun distinguishesMissingExtractedFileFromHashMismatch() = withDirectory { directory ->
        val bytes = byteArrayOf(5, 4, 3, 2, 1)
        val apk = zip(directory.resolve("module.apk"), listOf(ENTRY_ARM64 to bytes))
        val nativeDirectory = directory.resolve("native").apply { mkdirs() }

        val missing = assertFailsWith<IllegalStateException> {
            DexKitNativeLibraryVerifier.verify(apk, nativeDirectory, true, ABI, sha256(bytes))
        }
        assertTrue(missing.message.orEmpty().contains("Extracted module DexKit library is missing"))

        nativeDirectory.resolve("libdexkit.so").writeBytes(byteArrayOf(9, 9, 9))
        val mismatch = assertFailsWith<IllegalStateException> {
            DexKitNativeLibraryVerifier.verify(apk, nativeDirectory, true, ABI, sha256(bytes))
        }
        assertTrue(mismatch.message.orEmpty().contains("Extracted native library SHA-256 mismatch"))
        assertTrue(mismatch.message.orEmpty().contains(sha256(byteArrayOf(9, 9, 9))))
    }

    @Test
    fun rejectsAClassLoaderMismatchBeforeCallingTheExplicitPathLoader() = withDirectory { directory ->
        val bytes = byteArrayOf(1, 2, 3)
        val apk = zip(directory.resolve("module.apk"), listOf(ENTRY_ARM64 to bytes))
        val verified = verifier(apk, sha256(bytes))
        val moduleLoader = ClassLoader.getSystemClassLoader()
        var called = false

        val failure = assertFailsWith<IllegalStateException> {
            DexKitNativeLibraryVerifier.load(
                verified = verified,
                moduleClassLoader = moduleLoader,
                runtimeClassLoader = object : ClassLoader(moduleLoader) {},
                bridgeClassLoader = moduleLoader,
            ) { called = true }
        }

        assertTrue(failure.message.orEmpty().contains("runtime is not loaded by the module class loader"))
        assertFalse(called)
    }

    @Test
    fun rejectsBridgeFromTheHostClassLoaderBeforeCallingTheExplicitPathLoader() = withDirectory { directory ->
        val bytes = byteArrayOf(1, 2, 3)
        val apk = zip(directory.resolve("module.apk"), listOf(ENTRY_ARM64 to bytes))
        val verified = verifier(apk, sha256(bytes))
        val moduleLoader = ClassLoader.getSystemClassLoader()
        var called = false

        val failure = assertFailsWith<IllegalStateException> {
            DexKitNativeLibraryVerifier.load(
                verified = verified,
                moduleClassLoader = moduleLoader,
                runtimeClassLoader = moduleLoader,
                bridgeClassLoader = object : ClassLoader(moduleLoader) {},
            ) { called = true }
        }

        assertTrue(failure.message.orEmpty().contains("DexKit bridge is not loaded by the module class loader"))
        assertFalse(called)
    }

    private fun verifier(apk: File, expectedSha256: String) = DexKitNativeLibraryVerifier.verify(
        sourceApk = apk,
        nativeLibraryDir = null,
        extractNativeLibs = false,
        expectedAbi = ABI,
        expectedSha256 = expectedSha256,
    )

    private fun zip(path: File, entries: List<Pair<String, ByteArray>>, stored: Boolean = true): File {
        path.parentFile?.mkdirs()
        ZipOutputStream(Files.newOutputStream(path.toPath())).use { output ->
            entries.forEach { (name, bytes) ->
                val entry = ZipEntry(name)
                if (stored) {
                    entry.method = ZipEntry.STORED
                    entry.size = bytes.size.toLong()
                    entry.compressedSize = bytes.size.toLong()
                    entry.crc = CRC32().apply { update(bytes) }.value
                }
                output.putNextEntry(entry)
                output.write(bytes)
                output.closeEntry()
            }
        }
        return path
    }

    private fun replaceZipEntryName(file: File, oldName: String, newName: String) {
        val oldBytes = oldName.encodeToByteArray()
        val newBytes = newName.encodeToByteArray()
        require(oldBytes.size == newBytes.size)
        val archive = Files.readAllBytes(file.toPath())
        var matches = 0
        var offset = 0
        while (offset <= archive.size - oldBytes.size) {
            val found = (oldBytes.indices).all { archive[offset + it] == oldBytes[it] }
            if (found) {
                newBytes.copyInto(archive, offset)
                matches++
                offset += oldBytes.size
            } else {
                offset++
            }
        }
        check(matches == 2) { "Expected one ZIP local header and central directory entry, found $matches" }
        Files.write(file.toPath(), archive)
    }

    private fun sha256(bytes: ByteArray): String = java.security.MessageDigest.getInstance("SHA-256")
        .digest(bytes)
        .joinToString("") { "%02x".format(it) }

    private fun withDirectory(block: (File) -> Unit) {
        val directory = Files.createTempDirectory("dexkit-native-test").toFile()
        try {
            block(directory)
        } finally {
            directory.deleteRecursively()
        }
    }

    private companion object {
        const val ABI = "arm64-v8a"
        const val ENTRY_ARM64 = "lib/arm64-v8a/libdexkit.so"
    }
}
