package io.github.nku100.impad.runtime

import android.content.pm.ApplicationInfo
import android.util.Log
import io.github.nku100.impad.rules.RuleExecutionFingerprint
import java.io.File

/** Installs the complete WeChat plan before its startup delegate is constructed. */
internal class WeChatEarlyRuntime(
    private val registrar: HookRegistrar,
    private val moduleInfo: ApplicationInfo,
    private val moduleLoader: ClassLoader,
    private val log: ModuleLog,
) {
    @Volatile private var installer: SemanticRuntimeCoordinator? = null
    private fun retire() {
        val outcome = installer?.retireForNewGeneration() ?: return
        log.write(Log.ERROR, "RESTART_REQUIRED wechat: ${outcome.reason}", null)
    }

    fun install(applicationInfo: ApplicationInfo, defaultLoader: ClassLoader): HookRegistration {
        val monitored = TinkerClassLoaderBridge(registrar).installMonitoredWithContext(defaultLoader,
            onReady = { _, _ -> log.write(Log.INFO,
                "wechat-early-after phase=delegate_attached timeNs=${System.nanoTime()}", null) },
            onLaterReady = { retire() })
        val early = try { WeChatEarlyClassLoaderBridge(registrar).install(defaultLoader, { context, loader ->
            require(context.packageName == "com.tencent.mm") { "Unexpected WeChat startup Context" }
            log.write(Log.INFO, "wechat-early-after phase=early_enter timeNs=${System.nanoTime()}", null)
            log.write(Log.INFO, "rule-runtime event=tinker_ready elapsedRealtimeNs=${System.nanoTime()} " +
                "contextPackage=${context.packageName} loaderClass=${loader.javaClass.name}", null)
            val catalog = io.github.nku100.impad.rules.wechat.WeChatSemanticRules.catalog
            val fingerprint = RuleExecutionFingerprint.createReport(catalog.sha256, RuleExecutionFingerprint.loadManifest())
            val storage = File(checkNotNull(applicationInfo.dataDir), "files/impad")
            val native = AndroidDexKitRuntime.load(moduleInfo, moduleLoader)
            require(native.sourceCommit == fingerprint.dexkitSourceCommit &&
                native.apiSha256 == fingerprint.dexkitAndroidApiSha256 &&
                native.nativeSha256 == fingerprint.dexkitAndroidArm64NativeSha256)
            log.write(Log.INFO,
                "rule-runtime event=native_loaded elapsedRealtimeNs=${System.nanoTime()} " +
                    "apiSha256=${native.apiSha256} nativeSha256=${native.nativeSha256} " +
                    "nativeAbi=${native.nativeAbi} packagingMode=${native.packagingMode} " +
                    "sourceApkPath=${native.sourceApkPath} nativeLoadPath=${native.nativeLoadPath}", null)
            val current = SemanticRuntimeCoordinator(catalog,
                FilePlanCache(File(storage, "semantic-plan-cache")), fingerprint.cacheIdentitySha256, registrar,
                prepareHooks = { plan, methods ->
                    SemanticRuntimeLog.requireLoggable(plan)
                    SemanticHookStrategies().prepare(plan, methods)
                })
            installer = current
            val build = InstalledBuildIdentityReader().readForRules(applicationInfo,
                AndroidInstalledApkMetadataReader(context.packageManager))
            val outcome = current.install(build, loader, File(storage, "startup-snapshots"))
            when (outcome) {
                is SemanticRuntimeOutcome.Installed -> {
                    log.write(Log.INFO, SemanticRuntimeLog.planEvent(outcome.plan, outcome.cacheHit, outcome.attempts, System.nanoTime()), null)
                    log.write(Log.INFO, SemanticRuntimeLog.installedEvent(outcome.plan, context.packageName, System.nanoTime()), null)
                }
                else -> log.write(Log.ERROR, "wechat-early-after installation rejected: $outcome", null)
            }

        }, { retire() }, { error ->
            retire()
            log.write(Log.ERROR, "wechat-early-after phase=failed errorType=${error.javaClass.simpleName}", error)
        }) } catch (error: Throwable) {
            try { monitored.unhook() } catch (cleanup: Throwable) { error.addSuppressed(cleanup) }
            throw error
        }
        return HookRegistration {
            var failure: Throwable? = null
            for (registration in listOf(early, monitored)) {
                try { registration.unhook() } catch (error: Throwable) {
                    if (failure == null) failure = error else failure.addSuppressed(error)
                }
            }
            failure?.let { throw it }
        }
    }
}
