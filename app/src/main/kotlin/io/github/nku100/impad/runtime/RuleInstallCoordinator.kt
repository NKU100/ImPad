package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.rules.ResolvedPlan

data class RuleInstallCandidate(val inputFingerprint: String, val plan: ResolvedPlan)

sealed interface StableRuleInstallResult {
    data class Installed(val registrations: List<HookRegistration>) : StableRuleInstallResult
    data object NoPlan : StableRuleInstallResult
    data object InputsChangedRepeatedly : StableRuleInstallResult
    data class Failed(val reason: String) : StableRuleInstallResult
}

class RuleInstallCoordinator(
    private val currentInputFingerprint: () -> String,
    private val resolve: () -> RuleInstallCandidate?,
    private val install: (ResolvedPlan, () -> Boolean) -> PlanInstallOutcome,
) {
    fun install(): StableRuleInstallResult {
        repeat(MAX_ATTEMPTS) { attempt ->
            val candidate = resolve() ?: return StableRuleInstallResult.NoPlan
            when (val outcome = install(candidate.plan) {
                currentInputFingerprint() == candidate.inputFingerprint
            }) {
                is PlanInstallOutcome.Installed -> return StableRuleInstallResult.Installed(outcome.registrations)
                PlanInstallOutcome.StaleInputs -> if (attempt == MAX_ATTEMPTS - 1) {
                    return StableRuleInstallResult.InputsChangedRepeatedly
                }
                is PlanInstallOutcome.Failed -> return StableRuleInstallResult.Failed(outcome.reason)
            }
        }
        error("Unreachable rule installation state")
    }

    private companion object {
        const val MAX_ATTEMPTS = 2
    }
}
