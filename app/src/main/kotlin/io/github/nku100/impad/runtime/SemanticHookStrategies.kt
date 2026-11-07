package io.github.nku100.impad.runtime

import io.github.nku100.impad.apps.qq.QqHookInstaller
import io.github.nku100.impad.apps.wechat.WeChatHookCallbacks
import io.github.nku100.impad.apps.wechat.WeChatHookInstaller
import io.github.nku100.impad.apps.wecom.WeComHookCallbacks
import io.github.nku100.impad.apps.wecom.WeComHookInstaller
import io.github.nku100.impad.compat.rules.CompletePlan
import io.github.nku100.impad.rules.SemanticAppCatalogs
import java.lang.reflect.Method

class SemanticHookStrategies(
    private val weChatCallbacks: WeChatHookCallbacks = WeChatHookCallbacks(),
    private val weComCallbacks: WeComHookCallbacks = WeComHookCallbacks(),
) {
    fun prepare(plan: CompletePlan, methods: Map<String, Method>): List<HookSpec> {
        val appId = when (plan.contractId) {
            "qq_tablet_device_type" -> "qq"
            "wechat_tablet_login" -> "wechat"
            "wecom_local_tablet" -> "wecom"
            else -> throw IllegalArgumentException("Unknown semantic contract: ${plan.contractId}")
        }
        val catalog = SemanticAppCatalogs.forApp(appId)
        require(plan.contractId == catalog.contractId && plan.contractRevision == catalog.contractRevision) {
            "Unsupported semantic behavior contract"
        }
        val strategy = catalog.strategies.singleOrNull {
            it.id == plan.strategyId && it.revision == plan.strategyRevision
        } ?: throw IllegalArgumentException("Unsupported semantic callback strategy")
        val expectedRoles = strategy.roles.map { it.role }
        require(plan.hooks.map { it.role } == expectedRoles) { "Semantic plan roles are incomplete or out of order" }
        plan.hooks.zip(strategy.roles).forEach { (hook, role) ->
            io.github.nku100.impad.rules.wechat.validateHookConfiguration(hook, role)
            require(hook.phase == role.phase && (role.role == "inner" || hook.configuration == role.configuration)) {
                "Semantic plan callback phase or configuration changed"
            }
        }
        require(methods.keys == expectedRoles.toSet()) { "Bound semantic methods do not match the strategy roles" }

        return when (appId) {
            "qq" -> listOf(QqHookInstaller.prepareBound(methods.getValue("device_type")))
            "wechat" -> {
                val inner = plan.hooks.single { it.role == "inner" }
                val method = methods.getValue("inner")
                val owner = method.declaringClass
                val cache = owner.getDeclaredField(inner.configuration.getValue("cacheField").substringAfter("->").substringBefore(':'))
                val monitorDescriptor = inner.configuration.getValue("monitorField")
                val monitor = if (monitorDescriptor == inner.descriptor.substringBefore("->")) null else
                    owner.getDeclaredField(monitorDescriptor.substringAfter("->").substringBefore(':'))
                listOf(HookSpec(method, "wechat_inner_after", WeChatInnerAfter.bind(method, cache, monitor))) +
                    WeChatHookInstaller.prepareBound(methods.filterKeys { it != "inner" }, weChatCallbacks)
            }
            "wecom" -> WeComHookInstaller.prepareBound(methods, weComCallbacks)
            else -> error("Catalog selection is exhaustive")
        }
    }
}
