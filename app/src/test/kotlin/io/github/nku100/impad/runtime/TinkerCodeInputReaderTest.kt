package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.BuildIdentity
import java.io.File
import java.nio.file.Files
import kotlin.test.*
import org.junit.jupiter.api.Test

class TinkerCodeInputReaderTest {
    @Test
    fun unpatchedProcessKeepsInstalledApkInputs() = withBuild { build, _ ->
        val actual = TinkerCodeInputReader { build.apkFiles }.read(build, javaClass.classLoader!!)
        assertEquals(build, actual)
    }

    @Test
    fun loadedMergedPatchReplacesDexSearchInputsButKeepsPackageIdentity() = withBuild { build, patch ->
        val actual = TinkerCodeInputReader { listOf(patch, patch) + build.apkFiles }
            .read(build, javaClass.classLoader!!)
        assertEquals(listOf(patch.canonicalFile), actual.runtimeCodeFiles)
        assertEquals(build.identity, actual.identity)
        assertEquals(build.apkFiles, actual.apkFiles)
        assertEquals(build.apkFileSha256, actual.apkFileSha256)
        assertEquals(listOf(InstalledBuildIdentityReader().sha256(patch)), actual.runtimeCodeSha256)
        assertNotEquals(build.installFingerprint, actual.installFingerprint)
    }

    @Test
    fun patchReplacementChangesFingerprintEvenAtSamePath() = withBuild { build, patch ->
        val reader = TinkerCodeInputReader { listOf(patch) }
        val first = reader.read(build, javaClass.classLoader!!)
        patch.writeText("replacement")
        val next = reader.read(build, javaClass.classLoader!!)
        assertNotEquals(first.installFingerprint, next.installFingerprint)
        assertNotEquals(first.runtimeCodeSha256, next.runtimeCodeSha256)
    }

    @Test
    fun inactivePatchesOnDiskAreNotSelected() = withBuild { build, _ ->
        assertEquals(build, TinkerCodeInputReader { build.apkFiles }.read(build, javaClass.classLoader!!))
    }

    @Test
    fun missingActivePatchFailsClosed() = withBuild { build, patch ->
        patch.delete()
        assertFailsWith<IllegalArgumentException> {
            TinkerCodeInputReader { listOf(patch) }.read(build, javaClass.classLoader!!)
        }
    }

    @Test
    fun patchOutsidePackageDataFailsClosed() = withBuild { build, _ ->
        val outside = File(build.dataDirectory.parentFile, "tinker_classN.apk")
        assertFailsWith<IllegalArgumentException> {
            TinkerCodeInputReader { listOf(outside) }.read(build, javaClass.classLoader!!)
        }
    }

    @Test
    fun multipleActiveMergedPatchesFailClosed() = withBuild { build, patch ->
        val other = File(build.dataDirectory, "tinker/patch-other/dex/tinker_classN.apk")
        checkNotNull(other.parentFile).mkdirs()
        other.writeText("other")
        assertFailsWith<IllegalArgumentException> {
            TinkerCodeInputReader { listOf(patch, other) }.read(build, javaClass.classLoader!!)
        }
    }

    private fun withBuild(block: (InstalledBuild, File) -> Unit) {
        val root = Files.createTempDirectory("impad-tinker-inputs").toFile()
        try {
            val base = File(root, "base.apk").apply { writeText("base") }
            val data = File(root, "data").apply { mkdirs() }
            val patch = File(data, "tinker/patch-test/dex/tinker_classN.apk")
            checkNotNull(patch.parentFile).mkdirs()
            patch.writeText("patch")
            block(InstalledBuild(
                BuildIdentity("com.tencent.mm", "8.0.79", 3200, "arm64-v8a", "a".repeat(64), "b".repeat(64)),
                listOf(base), "base-fingerprint", data, listOf("a".repeat(64)),
            ), patch)
        } finally {
            root.deleteRecursively()
        }
    }
}
