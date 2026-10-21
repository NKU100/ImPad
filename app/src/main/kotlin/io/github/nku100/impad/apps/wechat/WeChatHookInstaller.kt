package io.github.nku100.impad.apps.wechat

import io.github.nku100.impad.runtime.HookCallback
import io.github.nku100.impad.runtime.HookInstaller
import io.github.nku100.impad.runtime.HookRegistrar
import io.github.nku100.impad.runtime.HookSpec
import io.github.nku100.impad.runtime.InstallOutcome
import io.github.nku100.impad.compat.rules.ResolvedPlan
import java.lang.reflect.Method

class WeChatHookInstaller(
    registrar: HookRegistrar,
    private val callbacks: WeChatHookCallbacks = WeChatHookCallbacks(),
) {
    private val installer = HookInstaller(registrar)

    fun install(plan: ResolvedPlan, methods: Map<String, Method>): InstallOutcome {
        if (!supports(plan)) return InstallOutcome.Failed("Unsupported WeChat rule or strategy")
        if (methods.keys != TARGET_IDS) return InstallOutcome.Failed("WeChat rule plan methods are incomplete")
        return installer.install(hookSpecs(methods.getValue("tablet"), methods.getValue("login")))
    }

    fun install(tabletMethod: Method, loginMethod: Method): InstallOutcome =
        installer.install(hookSpecs(tabletMethod, loginMethod))

    private fun hookSpecs(tabletMethod: Method, loginMethod: Method) = listOf(
        HookSpec(tabletMethod, "wechatpad_tablet", HookCallback(callbacks::tabletDetection)),
        HookSpec(loginMethod, "wechatpad_login", HookCallback(callbacks::loginEntry)),
    )

    companion object {
        private const val RULE_ID = "wechat_tablet_login_001"
        private const val STRATEGY_ID = "wechat_tablet_login_behavior_001"
        private val TARGET_IDS = setOf("tablet", "login")

        internal fun supports(plan: ResolvedPlan): Boolean =
            plan.rule.ruleId == RULE_ID && plan.rule.strategyId == STRATEGY_ID && plan.targets.keys == TARGET_IDS
    }
}
