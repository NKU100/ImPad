package io.github.nku100.impad.checker

import io.github.nku100.impad.compat.BuildIdentity
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import java.util.zip.ZipFile

class ApkIdentityInspector(
    private val sdkRoot: File = defaultAndroidSdkRoot(),
    private val commandRunner: (List<String>) -> String = ::runCommand,
) {
    fun inspect(apk: File, abiSplitApks: List<File> = emptyList()): BuildIdentity {
        require(apk.isFile) { "APK does not exist: ${apk.absolutePath}" }
        require(abiSplitApks.all(File::isFile)) { "An ABI split APK was missing" }

        val absoluteApk = apk.absoluteFile
        val apkanalyzer = executable(sdkRoot.resolve("cmdline-tools/latest/bin/apkanalyzer"))
        val apksigner = executable(latestBuildToolsDirectory().resolve("apksigner"))
        val packageName = manifestValue(apkanalyzer, "application-id", absoluteApk)
        val versionName = manifestValue(apkanalyzer, "version-name", absoluteApk)
        val versionCode = manifestValue(apkanalyzer, "version-code", absoluteApk).toLongOrNull()
            ?: throw IllegalArgumentException("APK manifest contains an invalid version code")

        return BuildIdentity(
            packageName = packageName,
            versionName = versionName,
            versionCode = versionCode,
            abi = readSingleAbi(listOf(absoluteApk) + abiSplitApks.map(File::getAbsoluteFile)),
            apkSha256 = sha256(absoluteApk),
            signerSha256 = readSingleSignerSha256(apksigner, absoluteApk),
        )
    }

    private fun manifestValue(apkanalyzer: File, value: String, apk: File): String {
        val output = commandRunner(listOf(
            apkanalyzer.absolutePath,
            "manifest",
            value,
            apk.absolutePath,
        )).trim()
        require(output.isNotEmpty()) { "apkanalyzer returned an empty $value" }
        return output
    }

    private fun readSingleAbi(apks: List<File>): String {
        val abis = apks.flatMap { apk ->
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
        require(abis.size == 1) { "Expected one APK native ABI, found ${abis.sorted()}" }
        return abis.single()
    }

    private fun readSingleSignerSha256(apksigner: File, apk: File): String {
        val output = commandRunner(listOf(
            apksigner.absolutePath,
            "verify",
            "--print-certs",
            apk.absolutePath,
        ))
        val digests = SIGNER_SHA256.findAll(output)
            .map { it.groupValues[1].replace(":", "").lowercase() }
            .toSet()
        require(digests.size == 1 && digests.single().length == 64) {
            "Expected one APK signing certificate SHA-256 digest, found ${digests.size}"
        }
        return digests.single()
    }

    private fun sha256(apk: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        apk.inputStream().buffered().use { input ->
            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    private fun latestBuildToolsDirectory(): File {
        val candidates = sdkRoot.resolve("build-tools").listFiles()
            .orEmpty()
            .filter { it.isDirectory && it.name.matches(BUILD_TOOLS_VERSION) }
        return candidates.maxWithOrNull { left, right -> compareVersions(left.name, right.name) }
            ?: throw IllegalStateException("No stable Android build-tools directory found under ${sdkRoot.absolutePath}")
    }

    private fun executable(file: File): File =
        if (isWindows() && File(file.path + ".bat").exists()) File(file.path + ".bat") else file

    private fun compareVersions(left: String, right: String): Int {
        val leftParts = left.split('.').map(String::toInt)
        val rightParts = right.split('.').map(String::toInt)
        for (index in 0 until maxOf(leftParts.size, rightParts.size)) {
            val comparison = leftParts.getOrElse(index) { 0 }.compareTo(rightParts.getOrElse(index) { 0 })
            if (comparison != 0) return comparison
        }
        return 0
    }

    private fun isWindows(): Boolean = System.getProperty("os.name").startsWith("Windows", ignoreCase = true)

    private companion object {
        val BUILD_TOOLS_VERSION = Regex("\\d+(?:\\.\\d+)*")
        val SIGNER_SHA256 = Regex("(?m)^(?!Source Stamp Signer:).*?certificate SHA-256 digest:\\s*([a-fA-F0-9:]+)$")
    }
}

private fun defaultAndroidSdkRoot(): File {
    val path = System.getenv("ANDROID_SDK_ROOT") ?: System.getenv("ANDROID_HOME")
        ?: throw IllegalStateException("Set ANDROID_SDK_ROOT or ANDROID_HOME to inspect an APK")
    return File(path)
}

private fun runCommand(command: List<String>): String {
    val capture = File.createTempFile("impad-apk-inspector-", ".log")
    try {
        val process = ProcessBuilder(command)
            .redirectErrorStream(true)
            .redirectOutput(capture)
            .start()
        if (!process.waitFor(30, TimeUnit.SECONDS)) {
            process.destroyForcibly()
            process.waitFor()
            throw IllegalStateException("Timed out running ${File(command.first()).name}")
        }
        val output = capture.readText()
        check(process.exitValue() == 0) {
            "${File(command.first()).name} failed: ${output.trim()}"
        }
        return output
    } finally {
        capture.delete()
    }
}
