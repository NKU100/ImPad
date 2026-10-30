package io.github.nku100.impad.runtime

import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import io.github.nku100.impad.compat.BuildIdentity
import java.io.File
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.util.zip.ZipFile

data class InstalledPackageMetadata(
    val packageName: String,
    val uid: Int,
    val apkFiles: List<File>,
    val dataDirectory: File,
)

data class InstalledBuild(
    val identity: BuildIdentity,
    val apkFiles: List<File>,
    val installFingerprint: String,
    val dataDirectory: File,
    val apkFileSha256: List<String> = emptyList(),
    val runtimeCodeFiles: List<File> = emptyList(),
    val runtimeCodeSha256: List<String> = emptyList(),
)

data class PackageApkIdentity(
    val packageName: String,
    val versionName: String,
    val versionCode: Long,
    val signerSha256: String,
)

interface InstalledApkMetadataReader {
    fun installed(packageName: String): PackageApkIdentity
    fun archive(apk: File): PackageApkIdentity?
    fun archives(apks: List<File>): List<PackageApkIdentity?> = apks.map(::archive)
}

internal fun packageArchiveInput(apks: List<File>): File {
    require(apks.isNotEmpty()) { "Installed APK set was empty" }
    val files = apks.map(File::getCanonicalFile)
    require(files.distinct().size == files.size) { "Installed APK set contained duplicate paths" }
    if (files.size == 1) return files.single()
    val directory = checkNotNull(files.first().parentFile)
    require(files.all { it.parentFile == directory }) { "Installed APKs did not share a package directory" }
    val cluster = directory.listFiles().orEmpty().filter { it.isFile && it.name.endsWith(".apk") }
        .map(File::getCanonicalFile).toSet()
    require(cluster == files.toSet()) { "Installed APK list did not match the package archive cluster" }
    return directory
}

class AndroidInstalledApkMetadataReader(private val packageManager: PackageManager) : InstalledApkMetadataReader {
    override fun installed(packageName: String): PackageApkIdentity {
        val info = packageManager.getPackageInfo(packageName, PackageManager.GET_SIGNING_CERTIFICATES)
        return info.toIdentity(packageName)
    }

    override fun archive(apk: File): PackageApkIdentity? = packageManager
        .getPackageArchiveInfo(apk.absolutePath, PackageManager.GET_SIGNING_CERTIFICATES)
        ?.toIdentity(null)

    override fun archives(apks: List<File>): List<PackageApkIdentity?> {
        // Android parses and verifies a split package as a directory; an individual split is not a base APK.
        val input = packageArchiveInput(apks)
        val identity = packageManager
            .getPackageArchiveInfo(input.absolutePath, PackageManager.GET_SIGNING_CERTIFICATES)
            ?.toIdentity(null)
        return apks.map { identity }
    }

    private fun android.content.pm.PackageInfo.toIdentity(expectedPackageName: String?): PackageApkIdentity {
        val name = packageName
        require(expectedPackageName == null || name == expectedPackageName) {
            "Installed package name did not match application info"
        }
        val signers = signingInfo?.apkContentsSigners.orEmpty()
        require(signers.size == 1) { "Package signing identity was missing or ambiguous" }
        val signerSha = MessageDigest.getInstance("SHA-256").digest(signers.single().toByteArray()).toHexString()
        return PackageApkIdentity(name, versionName.orEmpty(), longVersionCode, signerSha)
    }
}

class InstalledBuildIdentityReader(
    private val supportedAbisProvider: () -> List<String> = { android.os.Build.SUPPORTED_ABIS.toList() },
) {
    fun read(applicationInfo: ApplicationInfo): InstalledBuild {
        val baseApkPath = checkNotNull(applicationInfo.sourceDir) { "WeChat source APK path was null" }
        val apkFiles = listOf(baseApkPath) + applicationInfo.splitSourceDirs.orEmpty()
        return read(
            InstalledPackageMetadata(
                packageName = applicationInfo.packageName.orEmpty(),
                uid = applicationInfo.uid,
                apkFiles = apkFiles.map(::File),
                dataDirectory = File(checkNotNull(applicationInfo.dataDir) { "WeChat data directory was null" }),
            ),
        )
    }

    fun readForRules(
        applicationInfo: ApplicationInfo,
        packageMetadata: InstalledApkMetadataReader,
    ): InstalledBuild {
        val packageName = checkNotNull(applicationInfo.packageName?.takeIf(String::isNotBlank)) {
            "Installed package name was missing"
        }
        val baseApkPath = checkNotNull(applicationInfo.sourceDir?.takeIf(String::isNotBlank)) {
            "Installed base APK path was missing"
        }
        val apkFiles = (listOf(baseApkPath) + applicationInfo.splitSourceDirs.orEmpty()).map(::File)
        require(apkFiles.all { it.isFile && it.canRead() }) { "An installed APK or split was not readable" }
        val installed = packageMetadata.installed(packageName)
        require(installed.packageName == packageName && installed.versionCode > 0) {
            "Installed package identity was incomplete"
        }
        require(installed.signerSha256.matches(HEX_SHA256)) { "Installed package signer was missing or invalid" }
        val archives = packageMetadata.archives(apkFiles)
        require(archives.size == apkFiles.size) { "Installed archive verification omitted an APK" }
        apkFiles.zip(archives).forEach { (apk, verified) ->
            val archive = verified
                ?: error("Installed APK metadata could not be read: ${apk.name}")
            require(archive == installed) { "Installed APK or split identity did not match PackageManager: ${apk.name}" }
        }
        val hashes = apkFiles.map(::sha256)
        val identity = BuildIdentity(
            packageName = installed.packageName,
            versionName = installed.versionName,
            versionCode = installed.versionCode,
            abi = readAbi(apkFiles),
            apkSha256 = hashes.first(),
            signerSha256 = installed.signerSha256.lowercase(),
        )
        return InstalledBuild(
            identity = identity,
            apkFiles = apkFiles,
            installFingerprint = installFingerprint(packageName, installed, apkFiles, hashes),
            dataDirectory = File(checkNotNull(applicationInfo.dataDir) { "Installed data directory was missing" }),
            apkFileSha256 = hashes,
        )
    }

    fun read(metadata: InstalledPackageMetadata): InstalledBuild {
        require(metadata.apkFiles.isNotEmpty()) { "WeChat APK paths were empty" }
        require(metadata.apkFiles.all(File::isFile)) { "An installed WeChat APK path was missing" }

        val identity = BuildIdentity(
            packageName = metadata.packageName,
            versionName = "",
            versionCode = 0,
            abi = readAbi(metadata.apkFiles),
            apkSha256 = null,
            signerSha256 = "",
        )
        return InstalledBuild(
            identity = identity,
            apkFiles = metadata.apkFiles,
            installFingerprint = installFingerprint(metadata),
            dataDirectory = metadata.dataDirectory,
        )
    }

    fun sha256(apk: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        apk.inputStream().buffered().use { input ->
            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        return digest.digest().toHexString()
    }

    private fun readAbi(apkFiles: List<File>): String {
        val availableAbis = apkFiles.flatMap { apk ->
            ZipFile(apk).use { zip ->
                zip.entries().asSequence()
                    .filter { !it.isDirectory && it.name.endsWith(".so") }
                    .mapNotNull { entry ->
                        val parts = entry.name.split('/')
                        parts.getOrNull(1)?.takeIf { parts.firstOrNull() == "lib" }
                    }
                    .toList()
            }
        }.toSet()
        return supportedAbisProvider().firstOrNull(availableAbis::contains)
            ?: availableAbis.singleOrNull()
            ?: "unknown"
    }

    private fun installFingerprint(metadata: InstalledPackageMetadata): String {
        val details = buildString {
            append(metadata.packageName)
            append('|')
            append(metadata.uid)
            for (apk in metadata.apkFiles) {
                append('|')
                append(apk.absolutePath)
                append(':').append(sha256(apk))
            }
        }
        return MessageDigest.getInstance("SHA-256")
            .digest(details.toByteArray(StandardCharsets.UTF_8))
            .toHexString()
    }

    private fun installFingerprint(
        packageName: String,
        identity: PackageApkIdentity,
        apkFiles: List<File>,
        hashes: List<String>,
    ): String {
        val details = buildString {
            append(packageName).append('|').append(identity.versionCode).append('|').append(identity.signerSha256)
            apkFiles.zip(hashes).forEach { (apk, hash) -> append('|').append(apk.canonicalPath).append(':').append(hash) }
        }
        return MessageDigest.getInstance("SHA-256").digest(details.toByteArray(StandardCharsets.UTF_8)).toHexString()
    }

    private fun ByteArray.toHexString(): String = joinToString("") { "%02x".format(it) }

    private companion object {
        val HEX_SHA256 = Regex("[a-fA-F0-9]{64}")
    }
}

private fun ByteArray.toHexString(): String = joinToString("") { "%02x".format(it) }
