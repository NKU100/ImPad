package io.github.nku100.impad.runtime

import android.content.pm.ApplicationInfo
import android.os.Build
import android.os.Process
import io.github.nku100.impad.rules.RuleExecutionFingerprint
import java.io.File

data class AndroidDexKitIdentity(
    val sourceCommit: String,
    val apiSha256: String,
    val nativeSha256: String,
    val nativeAbi: String,
    val packagingMode: String,
    val sourceApkPath: String,
    val nativeLoadPath: String,
)

object AndroidDexKitRuntime {
    private const val MODULE_PACKAGE = "io.github.nku100.impad"
    private const val LOCKED_NATIVE_ABI = "arm64-v8a"
    private var loadedIdentity: AndroidDexKitIdentity? = null

    @Synchronized
    fun load(applicationInfo: ApplicationInfo, moduleClassLoader: ClassLoader): AndroidDexKitIdentity {
        check(applicationInfo.packageName == MODULE_PACKAGE) {
            "DexKit native source must be the ImPad module package; got ${applicationInfo.packageName}"
        }
        check(Process.is64Bit() && LOCKED_NATIVE_ABI in Build.SUPPORTED_ABIS) {
            "Locked Android DexKit native library requires a 64-bit $LOCKED_NATIVE_ABI process; " +
                "supportedAbis=${Build.SUPPORTED_ABIS.joinToString()}"
        }

        val manifest = RuleExecutionFingerprint.loadManifest()
        val expectedNative = manifest.dexkitAndroidArm64NativeSha256
        check(expectedNative.matches(Regex("[a-fA-F0-9]{64}"))) {
            "Locked Android DexKit native identity is missing"
        }
        check(manifest.dexkitAndroidApiSha256.matches(Regex("[a-fA-F0-9]{64}"))) {
            "Locked Android DexKit API identity is missing"
        }

        val sourceApk = File(checkNotNull(applicationInfo.sourceDir) {
            "Module source APK path was missing"
        })
        val nativeDirectory = applicationInfo.nativeLibraryDir
            ?.takeIf(String::isNotBlank)
            ?.let(::File)
        val extractNativeLibs = applicationInfo.flags and ApplicationInfo.FLAG_EXTRACT_NATIVE_LIBS != 0
        val verifiedNative = DexKitNativeLibraryVerifier.verify(
            sourceApk = sourceApk,
            nativeLibraryDir = nativeDirectory,
            extractNativeLibs = extractNativeLibs,
            expectedAbi = LOCKED_NATIVE_ABI,
            expectedSha256 = expectedNative,
        )

        val bridge = Class.forName("org.luckypray.dexkit.DexKitBridge", false, moduleClassLoader)
        val identity = AndroidDexKitIdentity(
            sourceCommit = manifest.dexkitSourceCommit,
            apiSha256 = manifest.dexkitAndroidApiSha256,
            nativeSha256 = verifiedNative.sha256,
            nativeAbi = verifiedNative.abi,
            packagingMode = verifiedNative.packagingMode,
            sourceApkPath = verifiedNative.sourceApkPath,
            nativeLoadPath = verifiedNative.loadPath,
        )

        val previousIdentity = loadedIdentity
        if (previousIdentity != null) {
            check(previousIdentity == identity) {
                "Android DexKit is already loaded from a different module native identity: " +
                    "loaded=${previousIdentity.nativeLoadPath} requested=${identity.nativeLoadPath}"
            }
        }
        DexKitNativeLibraryVerifier.load(
            verified = verifiedNative,
            moduleClassLoader = moduleClassLoader,
            runtimeClassLoader = AndroidDexKitRuntime::class.java.classLoader,
            bridgeClassLoader = bridge.classLoader,
            loadExplicitPath = { path ->
                if (previousIdentity == null) System.load(path)
            },
        )
        loadedIdentity = identity
        return identity
    }
}
