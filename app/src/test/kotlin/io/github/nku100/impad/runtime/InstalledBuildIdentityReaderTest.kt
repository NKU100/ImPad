package io.github.nku100.impad.runtime

import android.content.pm.ApplicationInfo
import java.nio.file.Files
import java.nio.file.attribute.FileTime
import java.util.Comparator
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNull
import org.junit.jupiter.api.Test

class InstalledBuildIdentityReaderTest {
    @Test
    fun readsPackageAbiFilesAndAnInstallFingerprintWithoutHashing() = withFixture { directory, apk ->
        val reader = InstalledBuildIdentityReader(supportedAbisProvider = { listOf("arm64-v8a") })

        val build = reader.read(
            InstalledPackageMetadata(
                packageName = "com.tencent.mm",
                uid = 10234,
                apkFiles = listOf(apk),
                dataDirectory = directory.resolve("data").toFile(),
            ),
        )

        assertEquals("com.tencent.mm", build.identity.packageName)
        assertEquals("arm64-v8a", build.identity.abi)
        assertNull(build.identity.apkSha256)
        assertEquals("", build.identity.signerSha256)
        assertEquals(64, reader.sha256(apk).length)
        assertEquals(64, build.installFingerprint.length)
    }

    @Test
    fun changesTheFingerprintWhenTheInstalledApkPathChanges() = withFixture { directory, apk ->
        val reader = InstalledBuildIdentityReader(supportedAbisProvider = { listOf("arm64-v8a") })
        val first = reader.read(
            InstalledPackageMetadata(
                packageName = "com.tencent.mm",
                uid = 10234,
                apkFiles = listOf(apk),
                dataDirectory = directory.resolve("data").toFile(),
            ),
        )
        val replacement = directory.resolve("replacement.apk").toFile()
        Files.copy(apk.toPath(), replacement.toPath())
        val second = reader.read(
            InstalledPackageMetadata(
                packageName = "com.tencent.mm",
                uid = 10234,
                apkFiles = listOf(replacement),
                dataDirectory = directory.resolve("data").toFile(),
            ),
        )

        assertEquals(false, first.installFingerprint == second.installFingerprint)
    }

    @Test
    fun changesTheFingerprintWhenApkBytesChangeWithoutChangingPathSizeOrMtime() = withFixture { directory, apk ->
        val reader = InstalledBuildIdentityReader(supportedAbisProvider = { listOf("arm64-v8a") })
        val metadata = InstalledPackageMetadata(
            packageName = "com.tencent.mm",
            uid = 10234,
            apkFiles = listOf(apk),
            dataDirectory = directory.resolve("data").toFile(),
        )
        val first = reader.read(metadata)
        val modified = apk.lastModified()
        val secondApk = directory.resolve("replacement.apk").toFile()
        ZipOutputStream(Files.newOutputStream(secondApk.toPath())).use { zip ->
            zip.putNextEntry(ZipEntry("lib/arm64-v8a/libwechat.so"))
            zip.write(byteArrayOf(3, 2, 1))
            zip.closeEntry()
        }
        assertEquals(apk.length(), secondApk.length())
        Files.copy(secondApk.toPath(), apk.toPath(), java.nio.file.StandardCopyOption.REPLACE_EXISTING)
        Files.setLastModifiedTime(apk.toPath(), FileTime.fromMillis(modified))

        val second = reader.read(metadata)

        assertEquals(false, first.installFingerprint == second.installFingerprint)
    }

    @Test
    fun ruleIdentityUsesPackageManagerVersionSignerAndEverySplit() = withFixture { directory, apk ->
        val split = directory.resolve("split_config.apk").toFile()
        Files.copy(apk.toPath(), split.toPath())
        val appInfo = applicationInfo(directory, apk, listOf(split))
        val signer = "a".repeat(64)
        val metadata = object : InstalledApkMetadataReader {
            override fun installed(packageName: String) =
                PackageApkIdentity(packageName, "9.3.1", 14378, signer)

            override fun archive(apk: java.io.File) =
                PackageApkIdentity("com.tencent.mobileqq", "9.3.1", 14378, signer)
        }

        val build = InstalledBuildIdentityReader(supportedAbisProvider = { listOf("arm64-v8a") })
            .readForRules(appInfo, metadata)

        assertEquals(14378L, build.identity.versionCode)
        assertEquals(signer, build.identity.signerSha256)
        assertEquals(2, build.apkFileSha256.size)
        assertEquals(build.apkFileSha256.first(), build.identity.apkSha256)
    }

    @Test
    fun rejectsMissingSignerAndSplitWithDifferentPackageVersionOrSigner() = withFixture { directory, apk ->
        val split = directory.resolve("split_config.apk").toFile()
        Files.copy(apk.toPath(), split.toPath())
        val appInfo = applicationInfo(directory, apk, listOf(split))
        val signer = "a".repeat(64)
        fun metadata(installedSigner: String, splitIdentity: PackageApkIdentity) = object : InstalledApkMetadataReader {
            override fun installed(packageName: String) =
                PackageApkIdentity(packageName, "9.3.1", 14378, installedSigner)

            override fun archive(apk: java.io.File) = if (apk == split) splitIdentity else
                PackageApkIdentity("com.tencent.mobileqq", "9.3.1", 14378, signer)
        }
        val reader = InstalledBuildIdentityReader(supportedAbisProvider = { listOf("arm64-v8a") })

        assertFailsWith<IllegalArgumentException> {
            reader.readForRules(appInfo, metadata("", PackageApkIdentity("com.tencent.mobileqq", "9.3.1", 14378, signer)))
        }
        for (identity in listOf(
            PackageApkIdentity("com.other.app", "9.3.1", 14378, signer),
            PackageApkIdentity("com.tencent.mobileqq", "9.3.1", 14379, signer),
            PackageApkIdentity("com.tencent.mobileqq", "9.3.1", 14378, "b".repeat(64)),
        )) {
            assertFailsWith<IllegalArgumentException> { reader.readForRules(appInfo, metadata(signer, identity)) }
        }
    }

    private fun applicationInfo(directory: java.nio.file.Path, base: java.io.File, splits: List<java.io.File>) =
        ApplicationInfo().apply {
            packageName = "com.tencent.mobileqq"
            sourceDir = base.absolutePath
            splitSourceDirs = splits.map(java.io.File::getAbsolutePath).toTypedArray()
            dataDir = directory.resolve("data").toString()
        }

    private fun withFixture(block: (java.nio.file.Path, java.io.File) -> Unit) {
        val directory = Files.createTempDirectory("impad installed build")
        try {
            val apk = directory.resolve("base.apk").toFile()
            ZipOutputStream(Files.newOutputStream(apk.toPath())).use { zip ->
                zip.putNextEntry(ZipEntry("lib/arm64-v8a/libwechat.so"))
                zip.write(byteArrayOf(1, 2, 3))
                zip.closeEntry()
            }
            block(directory, apk)
        } finally {
            Files.walk(directory).use { paths ->
                paths.sorted(Comparator.reverseOrder<java.nio.file.Path>()).forEach(Files::deleteIfExists)
            }
        }
    }

}
