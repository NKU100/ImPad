package io.github.nku100.impad

import android.content.pm.ApplicationInfo
import android.util.Log
import io.github.libxposed.api.XposedModule
import io.github.libxposed.api.XposedModuleInterface.ModuleLoadedParam
import io.github.libxposed.api.XposedModuleInterface.PackageLoadedParam
import io.github.nku100.impad.rules.RuleExecutionFingerprint
import io.github.nku100.impad.rules.SemanticAppCatalogs
import io.github.nku100.impad.runtime.AndroidDexKitRuntime
import io.github.nku100.impad.runtime.AndroidInstalledApkMetadataReader
import io.github.nku100.impad.runtime.AppAdapter
import io.github.nku100.impad.runtime.AppAdapterRegistry
import io.github.nku100.impad.runtime.FilePlanCache
import io.github.nku100.impad.runtime.InstalledBuildIdentityReader
import io.github.nku100.impad.runtime.LibXposedHookRegistrar
import io.github.nku100.impad.runtime.ModuleLog
import io.github.nku100.impad.runtime.SemanticHookStrategies
import io.github.nku100.impad.runtime.SemanticRuntimeCoordinator
import io.github.nku100.impad.runtime.SemanticRuntimeLog
import io.github.nku100.impad.runtime.SemanticRuntimeOutcome
import java.io.File
import java.util.concurrent.ConcurrentHashMap

class ImPadModule : XposedModule() {
    private var currentProcessName: String? = null
    private val handledPackages = ConcurrentHashMap.newKeySet<String>()
    private val adapters = AppAdapterRegistry()
    private val coordinators = ConcurrentHashMap<CoordinatorKey, SemanticRuntimeCoordinator>()

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
        if (!handledPackages.add(param.packageName)) return

        try {
            val registrar = LibXposedHookRegistrar(this)
            val moduleLog = ModuleLog { priority, message, error ->
                if (error == null) log(priority, message) else log(priority, LOG_TAG, message, error)
            }
            val key = CoordinatorKey(adapter.compatibilityPolicy.appId, checkNotNull(processName))
            adapter.prepareClassLoader(
                param.defaultClassLoader,
                registrar,
                onLaterReady = {
                    val coordinator = coordinators[key]
                    if (coordinator == null) {
                        moduleLog.write(
                            Log.ERROR,
                            "RESTART_REQUIRED ${key.appId} startup generation changed before its semantic coordinator was ready; fail closed",
                            null,
                        )
                    } else {
                        val outcome = coordinator.retireForNewGeneration()
                        moduleLog.write(Log.ERROR, "RESTART_REQUIRED ${key.appId}: ${outcome.reason}", null)
                    }
                },
                onReady = { context, readyLoader ->
                    initializeSemanticRuntime(
                        adapter, param.applicationInfo, context, readyLoader, key, registrar, moduleLog,
                    )
                },
            )
        } catch (error: Throwable) {
            logFailure("module initialization failed", error)
        }
    }

    private fun initializeSemanticRuntime(
        adapter: AppAdapter,
        applicationInfo: ApplicationInfo,
        context: android.content.Context,
        readyLoader: ClassLoader,
        key: CoordinatorKey,
        registrar: LibXposedHookRegistrar,
        moduleLog: ModuleLog,
    ) {
        try {
            require(context.packageName == adapter.packageName) {
                "Class-loader ready Context belonged to a different package"
            }
            moduleLog.write(
                Log.INFO,
                "rule-runtime event=${adapter.classLoaderReadyEvent} elapsedRealtimeNs=${System.nanoTime()} " +
                    "contextPackage=${context.packageName} loaderClass=${readyLoader.javaClass.name}",
                null,
            )
            val moduleInfo = checkNotNull(getModuleApplicationInfo()) { "Module application info was unavailable" }
            val nativeIdentity = AndroidDexKitRuntime.load(moduleInfo, javaClass.classLoader!!)
            val catalog = SemanticAppCatalogs.forApp(adapter.compatibilityPolicy.appId)
            val fingerprint = RuleExecutionFingerprint.createReport(
                catalog.sha256,
                RuleExecutionFingerprint.loadManifest(),
            )
            check(nativeIdentity.sourceCommit == fingerprint.dexkitSourceCommit &&
                nativeIdentity.apiSha256 == fingerprint.dexkitAndroidApiSha256 &&
                nativeIdentity.nativeSha256 == fingerprint.dexkitAndroidArm64NativeSha256) {
                "Loaded Android DexKit runtime did not match the generated execution fingerprint"
            }
            moduleLog.write(
                Log.INFO,
                "rule-runtime event=native_loaded elapsedRealtimeNs=${System.nanoTime()} " +
                    "apiSha256=${nativeIdentity.apiSha256} nativeSha256=${nativeIdentity.nativeSha256} " +
                    "nativeAbi=${nativeIdentity.nativeAbi} packagingMode=${nativeIdentity.packagingMode} " +
                    "sourceApkPath=${nativeIdentity.sourceApkPath} nativeLoadPath=${nativeIdentity.nativeLoadPath}",
                null,
            )

            val identityReader = InstalledBuildIdentityReader()
            val build = identityReader.readForRules(
                applicationInfo,
                AndroidInstalledApkMetadataReader(context.packageManager),
            )
            val strategyFactory = SemanticHookStrategies()
            val coordinator = coordinators.computeIfAbsent(key) {
                SemanticRuntimeCoordinator(
                    catalog = catalog,
                    cache = FilePlanCache(File(build.dataDirectory, "files/impad/semantic-plan-cache")),
                    executionIdentitySha256 = fingerprint.cacheIdentitySha256,
                    registrar = registrar,
                    prepareHooks = { plan, methods ->
                        SemanticRuntimeLog.requireLoggable(plan)
                        strategyFactory.prepare(plan, methods)
                    },
                    onCacheFailure = { error ->
                        moduleLog.write(Log.WARN, "${key.appId} semantic plan cache unavailable: ${error.message}", error)
                    },
                )
            }
            val outcome = coordinator.install(
                build,
                readyLoader,
                File(build.dataDirectory, "files/impad/startup-snapshots"),
            )
            reportSemanticOutcome(outcome, adapter, key.processName, moduleLog)
        } catch (error: Throwable) {
            if (error is VirtualMachineError || error is ThreadDeath) throw error
            moduleLog.write(
                Log.ERROR,
                "${adapter.compatibilityPolicy.appId} semantic runtime unavailable: ${error.message ?: error.javaClass.simpleName}",
                error,
            )
        }
    }

    private fun reportSemanticOutcome(
        outcome: SemanticRuntimeOutcome,
        adapter: AppAdapter,
        processName: String,
        moduleLog: ModuleLog,
    ) {
        when (outcome) {
            is SemanticRuntimeOutcome.Installed -> {
                moduleLog.write(
                    Log.INFO,
                    SemanticRuntimeLog.planEvent(outcome.plan, outcome.cacheHit, outcome.attempts, System.nanoTime()),
                    null,
                )
                val appName = when (adapter.compatibilityPolicy.appId) {
                    "qq" -> "QQ"
                    "wechat" -> "WeChat"
                    "wecom" -> "WeCom"
                    else -> adapter.compatibilityPolicy.appId
                }
                moduleLog.write(Log.INFO, "installed ${outcome.plan.hooks.size} $appName hooks", null)
                moduleLog.write(
                    Log.INFO,
                    SemanticRuntimeLog.installedEvent(outcome.plan, processName, System.nanoTime()),
                    null,
                )
            }
            is SemanticRuntimeOutcome.Rejected ->
                moduleLog.write(Log.ERROR, "${adapter.compatibilityPolicy.appId} semantic plan rejected: ${outcome.outcome}", null)
            is SemanticRuntimeOutcome.Failed -> moduleLog.write(
                Log.ERROR,
                "${adapter.compatibilityPolicy.appId} semantic installation failed: ${outcome.reason}" +
                    if (outcome.restartRequired) "; RESTART_REQUIRED" else "",
                null,
            )
            is SemanticRuntimeOutcome.InputsChangedRepeatedly ->
                moduleLog.write(Log.ERROR, "${adapter.compatibilityPolicy.appId} startup inputs changed repeatedly; installation not completed", null)
            is SemanticRuntimeOutcome.RestartRequired ->
                moduleLog.write(Log.ERROR, "RESTART_REQUIRED ${adapter.compatibilityPolicy.appId}: ${outcome.reason}", null)
        }
    }

    private fun log(priority: Int, message: String) {
        log(priority, LOG_TAG, message)
    }

    private fun logFailure(stage: String, error: Throwable) {
        if (error is VirtualMachineError || error is ThreadDeath) throw error
        log(Log.ERROR, LOG_TAG, "$stage: ${error.message ?: error.javaClass.simpleName}", error)
    }

    private data class CoordinatorKey(val appId: String, val processName: String)

    private companion object {
        const val LOG_TAG = "ImPad"
    }
}
