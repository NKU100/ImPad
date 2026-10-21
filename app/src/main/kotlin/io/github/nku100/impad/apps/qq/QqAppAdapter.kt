package io.github.nku100.impad.apps.qq

import android.util.Log
import android.content.Context
import io.github.nku100.impad.compat.apps.qq.QqCompatibilityPolicy
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.runtime.*

class QqAppAdapter : AppAdapter {
    override val compatibilityPolicy = QqCompatibilityPolicy.policy

    override fun acceptsProcess(processName: String?): Boolean = processName == "$packageName:MSF"

    override fun prepareClassLoader(
        loader: ClassLoader,
        registrar: HookRegistrar,
        onReady: (Context, ClassLoader) -> Unit,
    ) {
        val application = loader.loadClass("com.tencent.common.app.QFixApplicationImplProxy")
        val attach = application.getDeclaredMethod("attachBaseContext", Context::class.java)
        PostAttachClassLoaderBridge(registrar).installWithContext(
            attach,
            "impad_qq_classloader_bootstrap",
            onReady,
        )
    }

    override fun installPlan(
        loader: ClassLoader,
        plan: ResolvedPlan,
        registrar: HookRegistrar,
        log: ModuleLog,
        inputsStillCurrent: () -> Boolean,
    ): PlanInstallOutcome {
        return try {
            val hooks = QqHookInstaller().preparePlan(loader, plan)
            if (!inputsStillCurrent()) return PlanInstallOutcome.StaleInputs
            when (val result = HookInstaller(registrar).install(hooks)) {
                is InstallOutcome.Installed -> {
                    log.write(Log.INFO, "installed ${result.registrations.size} QQ hooks", null)
                    log.write(
                        Log.INFO,
                        "rule-runtime event=hook_installed elapsedRealtimeNs=${System.nanoTime()} " +
                            "ruleId=${plan.rule.ruleId} strategyId=${plan.rule.strategyId} " +
                            "hookCount=${result.registrations.size} processName=$packageName:MSF",
                        null,
                    )
                    PlanInstallOutcome.Installed(result.registrations)
                }
                is InstallOutcome.Failed -> {
                    log.write(Log.ERROR, "QQ rule hook installation failed: ${result.reason}", null)
                    PlanInstallOutcome.Failed(result.reason)
                }
            }
        } catch (error: Throwable) {
            if (error is VirtualMachineError || error is ThreadDeath) throw error
            log.write(Log.ERROR, "QQ rule plan preparation failed: ${error.message ?: error.javaClass.simpleName}", error)
            PlanInstallOutcome.Failed(error.message ?: error.javaClass.simpleName)
        }
    }
}
