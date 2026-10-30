package io.github.nku100.impad.compat.rules

import java.util.Collections

class RecognizedPlan(val plan: CompletePlan, evidence: List<String>) {
    val evidence: List<String> = frozenResultList(evidence)
}

sealed interface RecognitionResult {
    class NoMatch(diagnostics: List<String> = emptyList()) : RecognitionResult {
        val diagnostics: List<String> = frozenResultList(diagnostics)
    }
    class Plans(plans: List<RecognizedPlan>) : RecognitionResult {
        val plans: List<RecognizedPlan> = frozenResultList(plans)
        init { require(this.plans.isNotEmpty()) { "Use NoMatch for an exhausted search without complete plans" } }
    }
    class Incomplete(val reason: String, discoveredPlans: List<RecognizedPlan> = emptyList()) : RecognitionResult {
        val discoveredPlans: List<RecognizedPlan> = frozenResultList(discoveredPlans)
        init { require(reason.isNotBlank()) { "Incomplete analysis needs a reason" } }
    }
    class UnsupportedInput(diagnostics: List<String>) : RecognitionResult {
        val diagnostics: List<String> = frozenResultList(diagnostics)
    }
    class EvaluationError(diagnostics: List<String>) : RecognitionResult {
        val diagnostics: List<String> = frozenResultList(diagnostics)
    }
}

class SelectedPlanMatch(val plan: CompletePlan, evidenceByRecognizer: Map<String, List<String>>) {
    val evidenceByRecognizer: Map<String, List<String>> = Collections.unmodifiableMap(evidenceByRecognizer.toSortedMap()
        .mapValues { (_, evidence) -> Collections.unmodifiableList(evidence.distinct().sorted()) })
}

sealed interface CompletePlanSelection {
    data class Selected(val match: SelectedPlanMatch) : CompletePlanSelection
    data object NoMatch : CompletePlanSelection
    data class Conflict(val plans: List<SelectedPlanMatch>) : CompletePlanSelection
    data class Incomplete(val reasons: Map<String, String>, val discoveredPlans: List<SelectedPlanMatch>) : CompletePlanSelection
    data class UnsupportedInput(val diagnostics: Map<String, List<String>>) : CompletePlanSelection
    data class EvaluationError(val diagnostics: Map<String, List<String>>) : CompletePlanSelection
}

object CompletePlanSelector {
    fun select(results: Map<String, RecognitionResult>): CompletePlanSelection {
        require(results.keys.none(String::isBlank)) { "Recognizer IDs must be nonblank" }
        val ordered = results.toSortedMap()
        val errors = ordered.mapNotNull { (id, result) ->
            (result as? RecognitionResult.EvaluationError)?.let { id to it.diagnostics }
        }.toMap()
        if (errors.isNotEmpty()) return CompletePlanSelection.EvaluationError(frozenDiagnostics(errors))
        val unsupported = ordered.mapNotNull { (id, result) ->
            (result as? RecognitionResult.UnsupportedInput)?.let { id to it.diagnostics }
        }.toMap()
        if (unsupported.isNotEmpty()) return CompletePlanSelection.UnsupportedInput(frozenDiagnostics(unsupported))

        val matches = linkedMapOf<CompletePlan, MutableMap<String, MutableList<String>>>()
        ordered.forEach { (id, result) ->
            val plans = when (result) {
                is RecognitionResult.Plans -> result.plans
                is RecognitionResult.Incomplete -> result.discoveredPlans
                else -> emptyList()
            }
            plans.forEach { recognized ->
                matches.getOrPut(recognized.plan, ::linkedMapOf).getOrPut(id, ::mutableListOf).addAll(recognized.evidence)
            }
        }
        val distinct = Collections.unmodifiableList(matches.map { (plan, evidence) -> SelectedPlanMatch(plan, evidence) }
            .sortedBy { it.plan.executionSha256() })
        if (distinct.size > 1) return CompletePlanSelection.Conflict(distinct)
        val incomplete = ordered.mapNotNull { (id, result) ->
            (result as? RecognitionResult.Incomplete)?.let { id to it.reason }
        }.toMap()
        if (incomplete.isNotEmpty()) return CompletePlanSelection.Incomplete(Collections.unmodifiableMap(incomplete), distinct)
        return distinct.singleOrNull()?.let { CompletePlanSelection.Selected(it) } ?: CompletePlanSelection.NoMatch
    }

    private fun frozenDiagnostics(values: Map<String, List<String>>): Map<String, List<String>> =
        Collections.unmodifiableMap(values.mapValues { (_, diagnostics) -> Collections.unmodifiableList(diagnostics.toList()) })
}

private fun <T> frozenResultList(values: List<T>): List<T> = Collections.unmodifiableList(values.toList())
