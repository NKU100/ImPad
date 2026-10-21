package io.github.nku100.impad.apps.wecom

import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.runtime.HookCallback
import io.github.nku100.impad.runtime.HookInstaller
import io.github.nku100.impad.runtime.HookRegistrar
import io.github.nku100.impad.runtime.HookSpec
import io.github.nku100.impad.runtime.InstallOutcome
import java.lang.reflect.Method

class WeComHookInstaller(
    registrar: HookRegistrar,
    private val callbacks: WeComHookCallbacks = WeComHookCallbacks(),
) {
    private val installer = HookInstaller(registrar)

    fun install(plan: ResolvedPlan, methods: Map<String, Method>): InstallOutcome {
        if (!supports(plan)) return InstallOutcome.Failed("Unsupported WeCom rule or strategy")
        if (methods.keys != TARGET_IDS) return InstallOutcome.Failed("WeCom rule plan methods are incomplete")

        return installer.install(
            listOf(
                HookSpec(methods.getValue("device_type"), DEVICE_TYPE_HOOK_ID, HookCallback(callbacks::deviceType)),
                HookSpec(methods.getValue("app_pad"), APPLICATION_PAD_HOOK_ID, HookCallback(callbacks::applicationIsPad)),
                HookSpec(methods.getValue("service_pad"), SERVICE_PAD_HOOK_ID, HookCallback(callbacks::serviceIsPad)),
            ),
        )
    }

    companion object {
        private const val DEVICE_TYPE_HOOK_ID = "impad_wecom_device_type"
        private const val APPLICATION_PAD_HOOK_ID = "impad_wecom_app_pad"
        private const val SERVICE_PAD_HOOK_ID = "impad_wecom_service_pad"
        private const val LOCAL_RULE_ID = "wecom_local_001"
        private const val LOCAL_STRATEGY_ID = "wecom_local_tablet_001"
        private val TARGET_IDS = setOf("device_type", "app_pad", "service_pad")

        internal fun supports(plan: ResolvedPlan): Boolean =
            plan.rule.ruleId == LOCAL_RULE_ID && plan.rule.strategyId == LOCAL_STRATEGY_ID &&
                plan.targets.keys == TARGET_IDS
    }
}
