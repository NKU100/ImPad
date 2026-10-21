package io.github.nku100.impad.rules

import io.github.nku100.impad.compat.BuildIdentity
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.RuleDefinition
import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.compat.rules.RuleSelector
import io.github.nku100.impad.compat.rules.SelectionOutcome
import io.github.nku100.impad.compat.rules.VersionRouter
import java.util.concurrent.CancellationException

interface RuleQuery {
    val definition: RuleDefinition
    fun resolve(session: RuleSession): RuleResolution
}

data class SharedRuleRun(
    val routeCandidates: List<String>,
    val results: Map<String, RuleResolution>,
    val outcome: SelectionOutcome,
)

object SharedRuleResolver {
    fun resolve(identity: BuildIdentity, catalog: RuleCatalog, session: RuleSession): SelectionOutcome =
        resolveDetailed(identity, catalog, session).outcome

    fun resolveDetailed(identity: BuildIdentity, catalog: RuleCatalog, session: RuleSession): SharedRuleRun {
        catalog.policy.validateIdentity(identity)
        VersionRouter.validate(catalog.bindings, catalog.definitions, catalog.strategyIds)
        val candidates = VersionRouter.candidates(identity.versionCode, catalog.bindings)
        val byId = catalog.queries.associateBy { it.definition.ruleId }
        val results = linkedMapOf<String, RuleResolution>()
        candidates.forEach { ruleId ->
            val query = requireNotNull(byId[ruleId]) { "No query registered for routed rule $ruleId" }
            val queryResult = try {
                query.resolve(session)
            } catch (error: Exception) {
                if (error is CancellationException || error is InterruptedException) throw error
                RuleResolution.Error(
                    candidateCounts = emptyMap(),
                    diagnostics = listOf(
                        "Rule $ruleId query failed; candidate counts unavailable: ${error.javaClass.simpleName}: ${error.message.orEmpty()}",
                    ),
                )
            }
            results[ruleId] = validateResult(query.definition, queryResult)
        }
        return SharedRuleRun(candidates, results, RuleSelector.select(results))
    }

    private fun validateResult(definition: RuleDefinition, result: RuleResolution): RuleResolution {
        if (result is RuleResolution.Error) return result
        val required = definition.requiredTargetIds
        if (result.candidateCounts.keys != required || result.candidateCounts.values.any { it < 0 }) {
            return RuleResolution.Error(
                candidateCounts = result.candidateCounts,
                diagnostics = listOf("Rule ${definition.ruleId} returned invalid target candidate counts"),
            )
        }
        return when (result) {
            is RuleResolution.Missing -> if (result.candidateCounts.values.all { it == 0 }) result else {
                RuleResolution.Error(result.candidateCounts, listOf("Missing result contains candidates"))
            }
            is RuleResolution.Ambiguous -> if (result.candidateCounts.any { it.value > 1 }) result else {
                RuleResolution.Error(result.candidateCounts, listOf("Ambiguous result has no multi-candidate target"))
            }
            is RuleResolution.Error -> result
            is RuleResolution.Unique -> {
                val plan: ResolvedPlan = result.plan
                val valid = plan.rule == definition.ref && plan.targets.keys == required &&
                    result.candidateCounts.values.all { it == 1 } &&
                    plan.targets.values.all { it.descriptor.isNotBlank() }
                if (valid) result else RuleResolution.Error(
                    result.candidateCounts,
                    listOf("Unique plan does not match immutable rule ${definition.ruleId}"),
                )
            }
        }
    }
}
