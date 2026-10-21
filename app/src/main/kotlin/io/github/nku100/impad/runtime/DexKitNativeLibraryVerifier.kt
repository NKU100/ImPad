package io.github.nku100.impad.runtime

import java.io.File
import java.io.FileInputStream
import java.security.MessageDigest
import java.util.zip.ZipEntry
import java.util.zip.ZipFile

internal data class VerifiedDexKitNativeLibrary(
    val abi: String,
    val zipEntry: String,
    val packagingMode: String,
    val sourceApkPath: String,
    val loadPath: String,
    val sha256: String,
)

internal object DexKitNativeLibraryVerifier {
    private const val LIBRARY_NAME = "libdexkit.so"
    private val nativeEntryPattern = Regex("^lib/([^/]+)/${Regex.escape(LIBRARY_NAME)}$")

    fun verify(
        sourceApk: File,
        nativeLibraryDir: File?,
        extractNativeLibs: Boolean,
        expectedAbi: String,
        expectedSha256: String,
    ): VerifiedDexKitNativeLibrary {
        check(expectedSha256.matches(Regex("[a-fA-F0-9]{64}"))) {
            "Locked Android DexKit native identity is missing or malformed"
        }
        val apk = sourceApk.canonicalFile
        check(apk.isFile) { "Module source APK is missing: ${apk.path}" }

        val entries = try {
            ZipFile(apk).use { zip ->
                val found = mutableListOf<NativeEntry>()
                val allEntries = zip.entries()
                while (allEntries.hasMoreElements()) {
                    val entry = allEntries.nextElement()
                    val match = nativeEntryPattern.matchEntire(entry.name) ?: continue
                    if (!entry.isDirectory) {
                        found += NativeEntry(
                            name = entry.name,
                            abi = match.groupValues[1],
                            method = entry.method,
                            sha256 = zip.getInputStream(entry).use(::sha256),
                        )
                    }
                }
                found
            }
        } catch (failure: Exception) {
            throw IllegalStateException("Unable to inspect module source APK: ${apk.path}", failure)
        }

        check(entries.isNotEmpty()) {
            "Module source APK has no DexKit native entry named $LIBRARY_NAME"
        }
        val expectedAbiEntries = entries.filter { it.abi == expectedAbi }
        check(expectedAbiEntries.isNotEmpty()) {
            val packagedAbis = entries.map { it.abi }.distinct().sorted().joinToString()
            "Module APK DexKit native entry uses ABI(s) $packagedAbis; expected $expectedAbi"
        }
        check(expectedAbiEntries.size == 1) {
            "Module source APK has ambiguous DexKit native entries for ABI $expectedAbi: " +
                expectedAbiEntries.joinToString { it.name }
        }
        val entry = expectedAbiEntries.single()
        check(entry.sha256.equals(expectedSha256, ignoreCase = true)) {
            "APK native entry SHA-256 mismatch for ${entry.name}: expected=$expectedSha256 actual=${entry.sha256}"
        }

        if (!extractNativeLibs) {
            check(entry.method == ZipEntry.STORED) {
                "APK native entry ${entry.name} must be stored for direct APK loading"
            }
            return VerifiedDexKitNativeLibrary(
                abi = entry.abi,
                zipEntry = entry.name,
                packagingMode = "APK_DIRECT",
                sourceApkPath = apk.path,
                loadPath = "${apk.path}!/${entry.name}",
                sha256 = entry.sha256,
            )
        }

        val directory = checkNotNull(nativeLibraryDir) {
            "Extracted module native library directory is missing"
        }.canonicalFile
        check(directory.isDirectory) {
            "Extracted module native library directory does not exist: ${directory.path}"
        }
        val library = File(directory, LIBRARY_NAME)
        check(library.isFile) {
            "Extracted module DexKit library is missing: ${library.path}"
        }
        val canonicalLibrary = library.canonicalFile
        check(canonicalLibrary.parentFile == directory) {
            "Extracted module DexKit library resolves outside nativeLibraryDir: ${canonicalLibrary.path}"
        }
        val extractedSha256 = FileInputStream(canonicalLibrary).use(::sha256)
        check(extractedSha256.equals(expectedSha256, ignoreCase = true)) {
            "Extracted native library SHA-256 mismatch at ${canonicalLibrary.path}: " +
                "expected=$expectedSha256 actual=$extractedSha256"
        }
        return VerifiedDexKitNativeLibrary(
            abi = entry.abi,
            zipEntry = entry.name,
            packagingMode = "EXTRACTED_FILE",
            sourceApkPath = apk.path,
            loadPath = canonicalLibrary.path,
            sha256 = extractedSha256,
        )
    }

    fun load(
        verified: VerifiedDexKitNativeLibrary,
        moduleClassLoader: ClassLoader,
        runtimeClassLoader: ClassLoader?,
        bridgeClassLoader: ClassLoader?,
        loadExplicitPath: (String) -> Unit,
    ) {
        check(runtimeClassLoader === moduleClassLoader) {
            "DexKit runtime is not loaded by the module class loader"
        }
        check(bridgeClassLoader === moduleClassLoader) {
            "DexKit bridge is not loaded by the module class loader"
        }
        loadExplicitPath(verified.loadPath)
    }

    private data class NativeEntry(
        val name: String,
        val abi: String,
        val method: Int,
        val sha256: String,
    )

    private fun sha256(input: java.io.InputStream): String {
        val digest = MessageDigest.getInstance("SHA-256")
        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
        while (true) {
            val size = input.read(buffer)
            if (size < 0) break
            digest.update(buffer, 0, size)
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }
}
