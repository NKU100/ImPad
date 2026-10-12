package io.github.nku100.impad

import android.content.pm.ApplicationInfo
import android.util.Log
import io.github.libxposed.api.XposedModule
import io.github.libxposed.api.XposedModuleInterface.ModuleLoadedParam
import io.github.libxposed.api.XposedModuleInterface.PackageLoadedParam
import io.github.nku100.impad.compat.CompatibilityStatus
import io.github.nku100.impad.compat.CompatibilityTarget
import io.github.nku100.impad.apps.wechat.WeChatAppAdapter
import io.github.nku100.impad.apps.wecom.WeComAppAdapter
import io.github.nku100.impad.runtime.AppAdapter
import io.github.nku100.impad.runtime.AppAdapterRegistry
import io.github.nku100.impad.runtime.InstalledBuildIdentityReader
import io.github.nku100.impad.runtime.LibXposedHookRegistrar
import io.github.nku100.impad.runtime.RuntimeCompatibilityResolver
import io.github.nku100.impad.runtime.RuntimeResolutionCache
import io.github.nku100.impad.runtime.ModuleLog
import java.io.File
import java.nio.charset.StandardCharsets
import java.util.zip.ZipFile
import kotlinx.serialization.json.Json

class ImPadModule : XposedModule() {
    private var currentProcessName: String? = null
    private val handledPackages = mutableSetOf<String>()
    private val adapters = AppAdapterRegistry(
        listOfNotNull(WeChatAppAdapter(), WeComAppAdapter().takeIf { BuildConfig.DEBUG }),
    )

    override fun onModuleLoaded(param: ModuleLoadedParam) {
        super.onModuleLoaded(param)
        currentProcessName = param.processName
    }

    override fun onPackageLoaded(param: PackageLoadedParam) {
        super.onPackageLoaded(param)
        if (param.packageName in handledPackages) return
        val adapter = adapters.forPackage(param.packageName) ?: return

        val processName = currentProcessName
        if (adapters.find(param.packageName, processName) == null) {
            log(Log.INFO, "${adapter.packageName} process skipped: ${processName ?: "unknown"}")
            return
        }
        handledPackages += param.packageName

        try {
            initialize(adapter, param.applicationInfo, param.defaultClassLoader)
        } catch (error: Throwable) {
            logFailure("module initialization failed", error)
        }
    }

    private fun initialize(adapter: AppAdapter, applicationInfo: ApplicationInfo, defaultClassLoader: ClassLoader) {
        val build = InstalledBuildIdentityReader().read(applicationInfo)
        val localOnlyTargetsPath = "assets/compatibility-local/${adapter.compatibilityPolicy.appId}/targets.json"
        val useLocalOnlyTargets = BuildConfig.DEBUG && hasTargetAsset(localOnlyTargetsPath)
        val targets = readTargets(if (useLocalOnlyTargets) localOnlyTargetsPath else adapter.targetsAssetPath)
        val cacheDirectory = File(build.dataDirectory, "files/impad/compatibility")
        val resolution = RuntimeCompatibilityResolver(
            targets = targets,
            policy = adapter.compatibilityPolicy,
            cache = RuntimeResolutionCache(cacheDirectory),
            targetValidator = if (useLocalOnlyTargets) {
                adapter.compatibilityPolicy::validateLocalTargets
            } else {
                adapter.compatibilityPolicy::validateTargets
            },
        ).resolve(build)

        val target = resolution.target
        val profileVersion = target?.identity?.versionName ?: "unknown"
        log(
            Log.INFO,
            "build=$profileVersion package=${build.identity.packageName} abi=${build.identity.abi} " +
                "apkSha256=${target?.identity?.apkSha256 ?: "unknown"} " +
                "signerSha256=${target?.identity?.signerSha256 ?: "unknown"} " +
                "status=${resolution.result.status} cacheHit=${resolution.cacheHit} reason=${resolution.result.reason}",
        )
        if (resolution.result.status != CompatibilityStatus.COMPATIBLE || target == null) return

        adapter.install(
            defaultClassLoader,
            resolution.result.resolvedDescriptors,
            LibXposedHookRegistrar(this),
            ModuleLog { priority, message, error ->
                if (error == null) log(priority, message) else log(priority, LOG_TAG, message, error)
            },
        )
    }

    private fun readTargets(assetPath: String): List<CompatibilityTarget> {
        val moduleApk = checkNotNull(getModuleApplicationInfo().sourceDir) {
            "Module APK path was null"
        }
        return ZipFile(moduleApk).use { apk ->
            val profile = checkNotNull(apk.getEntry(assetPath)) {
                "Compatibility profiles were missing from the module APK"
            }
            val json = apk.getInputStream(profile).bufferedReader(StandardCharsets.UTF_8).use { it.readText() }
            Json.decodeFromString<List<CompatibilityTarget>>(json)
        }
    }

    private fun hasTargetAsset(assetPath: String): Boolean {
        val moduleApk = checkNotNull(getModuleApplicationInfo().sourceDir) {
            "Module APK path was null"
        }
        return ZipFile(moduleApk).use { it.getEntry(assetPath) != null }
    }

    private fun log(priority: Int, message: String) {
        log(priority, LOG_TAG, message)
    }

    private fun logFailure(stage: String, error: Throwable) {
        if (error is VirtualMachineError || error is ThreadDeath) throw error
        log(Log.ERROR, LOG_TAG, "$stage: ${error.message ?: error.javaClass.simpleName}", error)
    }

    private companion object {
        const val LOG_TAG = "ImPad"
    }
}
