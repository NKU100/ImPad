package io.github.nku100.impad.compat.rules

import kotlinx.serialization.Serializable

@Serializable
data class RuleRef(val ruleId: String, val strategyId: String)

@Serializable
data class VersionBinding(val ruleId: String, val min: Long, val max: Long?)

@Serializable
data class RuleDefinition(
    val ruleId: String,
    val strategyId: String,
    val requiredTargetIds: Set<String>,
) {
    val ref: RuleRef get() = RuleRef(ruleId, strategyId)
}

@Serializable
data class MethodShape(
    val parameters: List<String>,
    val returnType: String,
    val isStatic: Boolean,
)

@Serializable
data class ResolvedTarget(val descriptor: String, val shape: MethodShape)

@Serializable
data class ResolvedPlan(val rule: RuleRef, val targets: Map<String, ResolvedTarget>)

sealed interface RuleResolution {
    val candidateCounts: Map<String, Int>
    val diagnostics: List<String>

    data class Missing(
        override val candidateCounts: Map<String, Int> = emptyMap(),
        override val diagnostics: List<String> = emptyList(),
    ) : RuleResolution

    data class Unique(
        val plan: ResolvedPlan,
        override val candidateCounts: Map<String, Int> = emptyMap(),
        override val diagnostics: List<String> = emptyList(),
    ) : RuleResolution

    data class Ambiguous(
        override val candidateCounts: Map<String, Int>,
        override val diagnostics: List<String> = emptyList(),
    ) : RuleResolution

    data class Error(
        override val candidateCounts: Map<String, Int> = emptyMap(),
        override val diagnostics: List<String>,
    ) : RuleResolution
}

sealed interface SelectionOutcome {
    data class Selected(val plan: ResolvedPlan) : SelectionOutcome
    data object NoMatch : SelectionOutcome
    data class Ambiguous(val rules: Map<String, RuleResolution.Ambiguous>) : SelectionOutcome
    data class RuleConflict(val ruleIds: List<String>) : SelectionOutcome
    data class Error(val rules: Map<String, RuleResolution.Error>) : SelectionOutcome
}
