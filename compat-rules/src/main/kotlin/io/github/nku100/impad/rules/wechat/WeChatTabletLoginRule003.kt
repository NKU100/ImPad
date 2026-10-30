package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.compat.rules.RuleDefinition
import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.compat.rules.VersionBinding
import io.github.nku100.impad.rules.RuleCatalog
import io.github.nku100.impad.rules.RuleQuery
import io.github.nku100.impad.rules.RuleSession

/** Aggregate tablet check supporting both direct and reloaded cached results. */
object WeChatTabletLoginRule003 : RuleQuery {
    override val definition = RuleDefinition(
        "wechat_tablet_login_003", "wechat_tablet_login_behavior_003", setOf("tablet", "login"),
    )

    override fun resolve(session: RuleSession): RuleResolution =
        when (val result = WeChatTabletLoginRule002.resolve(session, reloadedCache = true, originalCacheAllowed = true)) {
            is RuleResolution.Unique -> result.copy(plan = result.plan.copy(rule = definition.ref))
            else -> result
        }

    val tinkerCatalog: RuleCatalog by lazy {
        val base = WeChatTabletLoginRule.catalog
        RuleCatalog(
            appId = base.appId,
            queries = base.queries,
            bindings = base.bindings.map { binding ->
                if (binding.ruleId == WeChatTabletLoginRule002.definition.ruleId) binding.copy(max = 3199)
                else binding
            } + VersionBinding(definition.ruleId, 3200, null),
            strategyIds = base.strategyIds,
            policy = base.policy,
        )
    }
}
