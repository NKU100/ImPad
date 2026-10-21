package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.rules.MethodShape
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.ResolvedTarget
import io.github.nku100.impad.compat.rules.RuleRef
import org.junit.jupiter.api.Test
import kotlin.test.assertEquals

class RuleInstallCoordinatorTest {
    @Test
    fun reparsesOnceWhenInputsChangeBeforeRegistration() {
        val identities = ArrayDeque(listOf("changed", "second"))
        var resolutions = 0
        val result = RuleInstallCoordinator(
            currentInputFingerprint = { identities.removeFirst() },
            resolve = { resolutions++; RuleInstallCandidate(if (resolutions == 1) "first" else "second", plan()) },
            install = { _, isCurrent ->
                if (isCurrent()) PlanInstallOutcome.Installed(emptyList()) else PlanInstallOutcome.StaleInputs
            },
        ).install()

        assertEquals(StableRuleInstallResult.Installed(emptyList()), result)
        assertEquals(2, resolutions)
    }

    @Test
    fun stopsAfterASecondInputChangeInsteadOfRetryingForever() {
        var resolutions = 0
        val identities = ArrayDeque(listOf("changed-one", "changed-two"))
        val result = RuleInstallCoordinator(
            currentInputFingerprint = { identities.removeFirst() },
            resolve = { resolutions++; RuleInstallCandidate("stable-$resolutions", plan()) },
            install = { _, isCurrent ->
                if (isCurrent()) PlanInstallOutcome.Installed(emptyList()) else PlanInstallOutcome.StaleInputs
            },
        ).install()

        assertEquals(StableRuleInstallResult.InputsChangedRepeatedly, result)
        assertEquals(2, resolutions)
    }

    private fun plan() = ResolvedPlan(
        RuleRef("qq_device_type_001", "qq_tablet_001"),
        mapOf("device_type" to ResolvedTarget("Lfixture/Target;->get()Z", MethodShape(emptyList(), "boolean", true))),
    )
}
