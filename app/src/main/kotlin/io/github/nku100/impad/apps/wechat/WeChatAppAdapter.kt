package io.github.nku100.impad.apps.wechat

import android.content.Context
import android.util.Log
import io.github.nku100.impad.compat.apps.wechat.WeChatCompatibilityPolicy
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.runtime.AppAdapter
import io.github.nku100.impad.runtime.HookRegistrar
import io.github.nku100.impad.runtime.InstallOutcome
import io.github.nku100.impad.runtime.ModuleLog
import io.github.nku100.impad.runtime.PlanInstallOutcome
import io.github.nku100.impad.runtime.TargetMethodResolver
import io.github.nku100.impad.runtime.TinkerClassLoaderBridge

class WeChatAppAdapter : AppAdapter {
    override val compatibilityPolicy = WeChatCompatibilityPolicy.policy
    override val classLoaderReadyEvent: String = "tinker_ready"

    override fun acceptsProcess(processName: String?): Boolean = processName == packageName

    override fun prepareClassLoader(
        loader: ClassLoader,
        registrar: HookRegistrar,
        onLaterReady: () -> Unit,
        onReady: (Context, ClassLoader) -> Unit,
    ) {
        TinkerClassLoaderBridge(registrar).installMonitoredWithContext(
            loader,
            WECHAT_TINKER_HOOK_ID,
            onReady = onReady,
            onLaterReady = onLaterReady,
        )
    }

    override fun installPlan(
        loader: ClassLoader,
        plan: ResolvedPlan,
        registrar: HookRegistrar,
        log: ModuleLog,
        inputsStillCurrent: () -> Boolean,
    ): PlanInstallOutcome {
        if (!WeChatHookInstaller.supports(plan)) {
            return PlanInstallOutcome.Failed("Unsupported WeChat rule or strategy")
        }
        return try {
            val methods = plan.targets.mapValues { (_, target) ->
                TargetMethodResolver.resolve(loader, target.descriptor, target.shape)
            }
            if (!inputsStillCurrent()) return PlanInstallOutcome.StaleInputs
            when (val result = WeChatHookInstaller(registrar).install(plan, methods)) {
                is InstallOutcome.Installed -> {
                    log.write(Log.INFO, "installed ${result.registrations.size} WeChat hooks", null)
                    log.write(
                        Log.INFO,
                        "rule-runtime event=hook_installed elapsedRealtimeNs=${System.nanoTime()} " +
                            "ruleId=${plan.rule.ruleId} strategyId=${plan.rule.strategyId} " +
                            "hookCount=${result.registrations.size} processName=$packageName",
                        null,
                    )
                    PlanInstallOutcome.Installed(result.registrations)
                }
                is InstallOutcome.Failed -> {
                    log.write(Log.ERROR, "WeChat rule hook installation failed: ${result.reason}", null)
                    PlanInstallOutcome.Failed(result.reason)
                }
            }
        } catch (error: Throwable) {
            if (error is VirtualMachineError || error is ThreadDeath) throw error
            log.write(Log.ERROR, "WeChat rule plan preparation failed: ${error.message ?: error.javaClass.simpleName}", error)
            PlanInstallOutcome.Failed(error.message ?: error.javaClass.simpleName)
        }
    }

    private companion object {
        const val WECHAT_TINKER_HOOK_ID = "impad_wechat_tinker_bootstrap"
    }
}
