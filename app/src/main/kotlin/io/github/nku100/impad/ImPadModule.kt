package io.github.nku100.impad

import android.util.Log
import android.content.pm.ApplicationInfo
import io.github.libxposed.api.XposedModule
import io.github.libxposed.api.XposedModuleInterface.ModuleLoadedParam
import io.github.libxposed.api.XposedModuleInterface.PackageLoadedParam
import io.github.nku100.impad.compat.rules.SelectionOutcome
import io.github.nku100.impad.rules.RuleCatalog
import io.github.nku100.impad.rules.RuleExecutionFingerprint
import io.github.nku100.impad.runtime.AndroidDexKitRuntime
import io.github.nku100.impad.runtime.AndroidInstalledApkMetadataReader
import io.github.nku100.impad.runtime.AppAdapter
import io.github.nku100.impad.runtime.AppAdapterRegistry
import io.github.nku100.impad.runtime.InstalledBuildIdentityReader
import io.github.nku100.impad.runtime.LibXposedHookRegistrar
import io.github.nku100.impad.runtime.RuleInstallCandidate
import io.github.nku100.impad.runtime.RuleInstallCoordinator
import io.github.nku100.impad.runtime.RuleResolutionCache
import io.github.nku100.impad.runtime.RuleRuntimeResolver
import io.github.nku100.impad.runtime.StableRuleInstallResult
import io.github.nku100.impad.runtime.ModuleLog
import java.io.File

class ImPadModule : XposedModule() {
    private var currentProcessName: String? = null
    private val handledPackages = mutableSetOf<String>()
    private val adapters = AppAdapterRegistry()

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
            val registrar = LibXposedHookRegistrar(this)
            val moduleLog = ModuleLog { priority, message, error ->
                if (error == null) log(priority, message) else log(priority, LOG_TAG, message, error)
            }
            adapter.prepareClassLoader(
                param.defaultClassLoader,
                registrar,
            ) { context, readyLoader ->
                moduleLog.write(
                    Log.INFO,
                    "rule-runtime event=${adapter.classLoaderReadyEvent} elapsedRealtimeNs=${System.nanoTime()} " +
                        "contextPackage=${context.packageName} loaderClass=${readyLoader.javaClass.name}",
                    null,
                )
                initializeRules(adapter, param.applicationInfo, context, readyLoader, registrar, moduleLog)
            }
        } catch (error: Throwable) {
            logFailure("module initialization failed", error)
        }
    }

    private fun initializeRules(
        adapter: AppAdapter,
        applicationInfo: ApplicationInfo,
        context: android.content.Context,
        readyLoader: ClassLoader,
        registrar: LibXposedHookRegistrar,
        moduleLog: ModuleLog,
    ) {
        try {
            val moduleInfo = checkNotNull(getModuleApplicationInfo()) { "Module application info was unavailable" }
            val nativeIdentity = AndroidDexKitRuntime.load(moduleInfo, javaClass.classLoader!!)
            val catalog = RuleCatalog.forApp(adapter.compatibilityPolicy.appId)
            val fingerprint = RuleExecutionFingerprint.createReport(catalog.sha256, RuleExecutionFingerprint.loadManifest())
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
            val packageMetadata = AndroidInstalledApkMetadataReader(context.packageManager)
            require(context.packageName == adapter.packageName) {
                "Class-loader ready Context belonged to a different package"
            }
            val resolver = RuleRuntimeResolver(
                catalog = catalog,
                cache = RuleResolutionCache(File(applicationInfo.dataDir, "files/impad/rules")),
                executionIdentity = fingerprint,
            )

            val result = RuleInstallCoordinator(
                currentInputFingerprint = {
                    identityReader.readForRules(applicationInfo, packageMetadata).installFingerprint
                },
                resolve = {
                    val build = identityReader.readForRules(applicationInfo, packageMetadata)
                    val resolution = resolver.resolve(build)
                    val plan = (resolution.outcome as? SelectionOutcome.Selected)?.plan
                    if (plan == null) {
                        moduleLog.write(Log.ERROR, "${adapter.compatibilityPolicy.appId} rule resolution failed: ${resolution.outcome}", null)
                        null
                    } else {
                        moduleLog.write(
                            Log.INFO,
                            "status=COMPATIBLE rule plan=${plan.rule.ruleId}/${plan.rule.strategyId} " +
                                "versionCode=${build.identity.versionCode} " +
                                "apkInputs=${build.apkFileSha256.size} cacheHit=${resolution.cacheHit}",
                            null,
                        )
                        RuleInstallCandidate(build.installFingerprint, plan)
                    }
                },
                install = { plan, stillCurrent ->
                    adapter.installPlan(readyLoader, plan, registrar, moduleLog, stillCurrent)
                },
            ).install()
            when (result) {
                is StableRuleInstallResult.Installed -> Unit
                StableRuleInstallResult.NoPlan -> Unit
                StableRuleInstallResult.InputsChangedRepeatedly ->
                    moduleLog.write(Log.ERROR, "${adapter.compatibilityPolicy.appId} installed APK inputs changed repeatedly during rule installation", null)
                is StableRuleInstallResult.Failed ->
                    moduleLog.write(Log.ERROR, "${adapter.compatibilityPolicy.appId} rule plan installation failed: ${result.reason}", null)
            }
        } catch (error: Throwable) {
            if (error is VirtualMachineError || error is ThreadDeath) throw error
            moduleLog.write(Log.ERROR, "${adapter.compatibilityPolicy.appId} rule runtime unavailable: ${error.message ?: error.javaClass.simpleName}", error)
        }
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
