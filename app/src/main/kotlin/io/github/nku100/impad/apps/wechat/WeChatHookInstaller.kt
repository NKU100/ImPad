package io.github.nku100.impad.apps.wechat

import io.github.nku100.impad.runtime.HookCallback
import io.github.nku100.impad.runtime.HookInstaller
import io.github.nku100.impad.runtime.HookRegistrar
import io.github.nku100.impad.runtime.HookSpec
import io.github.nku100.impad.runtime.InstallOutcome
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.RuleRef
import java.lang.reflect.Method

class WeChatHookInstaller(
    registrar: HookRegistrar,
    private val callbacks: WeChatHookCallbacks = WeChatHookCallbacks(),
) {
    private val installer = HookInstaller(registrar)

    fun install(plan: ResolvedPlan, methods: Map<String, Method>): InstallOutcome {
        if (!supports(plan)) return InstallOutcome.Failed("Unsupported WeChat rule or strategy")
        if (methods.keys != TARGET_IDS) return InstallOutcome.Failed("WeChat rule plan methods are incomplete")
        val specs = hookSpecsForPlan(plan, methods.getValue("tablet"), methods.getValue("login"))
            ?: return InstallOutcome.Failed("Unsupported WeChat rule or strategy")
        return installer.install(specs)
    }

    fun install(tabletMethod: Method, loginMethod: Method): InstallOutcome =
        installer.install(hookSpecs(tabletMethod, loginMethod))

    private fun hookSpecs(tabletMethod: Method, loginMethod: Method) = prepareBound(tabletMethod, loginMethod, callbacks)

    private fun hookSpecsForPlan(plan: ResolvedPlan, tabletMethod: Method, loginMethod: Method) =
        when (plan.rule) {
            RuleRef(RULE_ID, STRATEGY_ID), RuleRef(RULE_ID_002, STRATEGY_ID_002),
            RuleRef(RULE_ID_003, STRATEGY_ID_003) ->
                hookSpecs(tabletMethod, loginMethod)
            else -> null
        }

    companion object {
        private const val RULE_ID = "wechat_tablet_login_001"
        private const val STRATEGY_ID = "wechat_tablet_login_behavior_001"
        private const val RULE_ID_002 = "wechat_tablet_login_002"
        private const val STRATEGY_ID_002 = "wechat_tablet_login_behavior_002"
        private const val RULE_ID_003 = "wechat_tablet_login_003"
        private const val STRATEGY_ID_003 = "wechat_tablet_login_behavior_003"
        private val TARGET_IDS = setOf("tablet", "login")

        internal fun prepareBound(methods: Map<String, Method>, callbacks: WeChatHookCallbacks) =
            prepareBound(methods.getValue("tablet"), methods.getValue("login"), callbacks)

        private fun prepareBound(tabletMethod: Method, loginMethod: Method, callbacks: WeChatHookCallbacks) = listOf(
            HookSpec(tabletMethod, "wechatpad_tablet", HookCallback(callbacks::tabletDetection)),
            HookSpec(loginMethod, "wechatpad_login", HookCallback(callbacks::loginEntry)),
        )

        internal fun supports(plan: ResolvedPlan): Boolean =
            (plan.rule in setOf(RuleRef(RULE_ID, STRATEGY_ID), RuleRef(RULE_ID_002, STRATEGY_ID_002),
                RuleRef(RULE_ID_003, STRATEGY_ID_003))) &&
                plan.targets.keys == TARGET_IDS
    }
}
