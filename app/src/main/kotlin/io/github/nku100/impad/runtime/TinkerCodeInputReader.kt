package io.github.nku100.impad.runtime

import java.io.File
import java.lang.reflect.Field
import java.security.MessageDigest

class TinkerCodeInputReader(
    private val loadedDexFiles: (ClassLoader) -> List<File> = ::readLoadedDexFiles,
) {
    fun read(build: InstalledBuild, readyLoader: ClassLoader): InstalledBuild {
        val tinkerRoot = File(build.dataDirectory, "tinker").canonicalFile.toPath()
        val loaded = loadedDexFiles(readyLoader).map(File::getCanonicalFile).distinct()
        val patches = loaded.filter { it.name == "tinker_classN.apk" || it.toPath().startsWith(tinkerRoot) }
        if (patches.isEmpty()) return build
        require(patches.size == 1) { "Multiple active Tinker code inputs are unsupported" }
        val patch = patches.single()
        require(patch.toPath().startsWith(tinkerRoot) &&
            tinkerRoot.relativize(patch.toPath()).toString().matches(MERGED_PATCH_PATH)) {
            "Active Tinker code input was not a merged archive in the package Tinker directory"
        }
        require(patch.isFile && patch.canRead()) { "Active Tinker code input was not readable" }
        val hash = InstalledBuildIdentityReader().sha256(patch)
        val fingerprint = MessageDigest.getInstance("SHA-256")
            .digest("${build.installFingerprint}|${patch.path}:$hash".toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
        // Tinker has already merged base and patch DEX. Scanning base as well would resurrect superseded methods.
        return build.copy(
            installFingerprint = fingerprint,
            runtimeCodeFiles = listOf(patch),
            runtimeCodeSha256 = listOf(hash),
        )
    }

    private companion object {
        val MERGED_PATCH_PATH = Regex("patch-[A-Za-z0-9]+/dex/tinker_classN\\.apk")

        fun readLoadedDexFiles(loader: ClassLoader): List<File> {
            val files = mutableListOf<File>()
            var current: ClassLoader? = loader
            while (current != null) {
                val pathListField = field(current.javaClass, "pathList")
                if (pathListField != null) {
                    val pathList = checkNotNull(pathListField.get(current))
                    val elements = checkNotNull(field(pathList.javaClass, "dexElements")).get(pathList) as Array<*>
                    for (element in elements.filterNotNull()) {
                        val dexFile = checkNotNull(field(element.javaClass, "dexFile")).get(element) ?: continue
                        val name = dexFile.javaClass.getMethod("getName").invoke(dexFile) as? String
                        if (!name.isNullOrBlank()) files += File(name)
                    }
                }
                current = current.parent
            }
            check(files.isNotEmpty()) { "Ready class loader did not expose its DEX inputs" }
            return files
        }

        fun field(type: Class<*>, name: String): Field? {
            var current: Class<*>? = type
            while (current != null) {
                try {
                    return current.getDeclaredField(name).apply { isAccessible = true }
                } catch (_: NoSuchFieldException) {
                    current = current.superclass
                }
            }
            return null
        }
    }
}
