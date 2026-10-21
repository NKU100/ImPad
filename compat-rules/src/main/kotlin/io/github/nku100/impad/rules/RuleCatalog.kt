package io.github.nku100.impad.rules

import io.github.nku100.impad.compat.AppCompatibilityPolicies
import io.github.nku100.impad.compat.AppCompatibilityPolicy
import io.github.nku100.impad.compat.rules.RuleDefinition
import io.github.nku100.impad.compat.rules.VersionBinding
import io.github.nku100.impad.compat.rules.VersionRouter
import io.github.nku100.impad.rules.qq.QqDeviceTypeRule
import io.github.nku100.impad.rules.wecom.WeComLocalRule
import io.github.nku100.impad.rules.wechat.WeChatTabletLoginRule
import java.security.MessageDigest

class RuleCatalog(
    val appId: String,
    val queries: List<RuleQuery>,
    val bindings: List<VersionBinding>,
    val strategyIds: Set<String>,
    val policy: AppCompatibilityPolicy = AppCompatibilityPolicies.require(appId),
) {
    val definitions: List<RuleDefinition> = queries.map(RuleQuery::definition)
    val sha256: String

    init {
        require(appId == policy.appId) { "Rule catalog appId does not match its compatibility policy" }
        require(queries.map { it.definition.ruleId }.toSet().size == queries.size) { "Duplicate rule query" }
        VersionRouter.validate(bindings, definitions, strategyIds)
        sha256 = digest(
            buildString {
                append(appId).append('\n')
                definitions.forEach { definition ->
                    append(definition.ruleId).append('|').append(definition.strategyId).append('|')
                    append(definition.requiredTargetIds.sorted().joinToString(",")).append('\n')
                }
                bindings.forEach { binding ->
                    append(binding.ruleId).append('|').append(binding.min).append('|').append(binding.max ?: "∞").append('\n')
                }
                strategyIds.sorted().forEach { append("strategy|").append(it).append('\n') }
            },
        )
    }

    fun query(ruleId: String): RuleQuery? = queries.firstOrNull { it.definition.ruleId == ruleId }

    companion object {
        fun forApp(appId: String): RuleCatalog = when (appId) {
            "qq" -> QqDeviceTypeRule.catalog
            "wechat" -> WeChatTabletLoginRule.catalog
            "wecom" -> WeComLocalRule.catalog
            else -> throw IllegalArgumentException("No feature rule catalog is registered for appId: $appId")
        }
    }
}

private fun digest(value: String): String = MessageDigest.getInstance("SHA-256")
    .digest(value.toByteArray(Charsets.UTF_8))
    .joinToString("") { "%02x".format(it) }
