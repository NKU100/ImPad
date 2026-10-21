package io.github.nku100.impad.compat.rules

import kotlin.test.Test
import kotlin.test.assertEquals

class RuleSelectorTest {
    private val plan = ResolvedPlan(
        RuleRef("rule", "strategy"),
        mapOf("target" to ResolvedTarget("Lsample/Target;->run()V", MethodShape(emptyList(), "V", true))),
    )

    @Test
    fun selectsOnlyOneCompleteUniqueRuleWhenEveryOtherRuleIsMissing() {
        val counts = mapOf("first" to 1, "second" to 0)
        assertEquals(
            SelectionOutcome.Selected(plan),
            RuleSelector.select(mapOf(
                "first" to RuleResolution.Unique(plan, counts),
                "second" to RuleResolution.Missing(mapOf("target" to 0)),
            )),
        )
    }

    @Test
    fun reportsNoMatchForEmptyResultsOrOnlyMissingRules() {
        assertEquals(SelectionOutcome.NoMatch, RuleSelector.select(emptyMap()))
        assertEquals(
            SelectionOutcome.NoMatch,
            RuleSelector.select(mapOf("rule" to RuleResolution.Missing(mapOf("target" to 0)))),
        )
    }

    @Test
    fun conflictsWhenTwoRulesAreUniqueEvenWhenTheirPlansMatch() {
        assertEquals(
            SelectionOutcome.RuleConflict(listOf("first", "second")),
            RuleSelector.select(linkedMapOf(
                "second" to RuleResolution.Unique(plan),
                "first" to RuleResolution.Unique(plan),
            )),
        )
    }

    @Test
    fun doesNotCombinePartialPlansAndPreservesActualCandidateCounts() {
        val firstPlan = ResolvedPlan(plan.rule, mapOf("one" to plan.targets.getValue("target")))
        val secondPlan = ResolvedPlan(plan.rule, mapOf("two" to plan.targets.getValue("target")))
        val counts = mapOf("one" to 3, "two" to 1)
        val result = RuleSelector.select(mapOf(
            "first" to RuleResolution.Unique(firstPlan, counts),
            "second" to RuleResolution.Unique(secondPlan, counts),
        ))
        assertEquals(SelectionOutcome.RuleConflict(listOf("first", "second")), result)
        assertEquals(counts, (RuleResolution.Unique(firstPlan, counts)).candidateCounts)
    }

    @Test
    fun ambiguityAndErrorInAnyRulePreventSelectionAndRetainDiagnostics() {
        val ambiguous = RuleResolution.Ambiguous(mapOf("target" to 2), listOf("two definitions"))
        val error = RuleResolution.Error(mapOf("target" to 0), listOf("input unavailable"))
        assertEquals(
            SelectionOutcome.Ambiguous(mapOf("second" to ambiguous)),
            RuleSelector.select(mapOf("first" to RuleResolution.Unique(plan), "second" to ambiguous)),
        )
        assertEquals(
            SelectionOutcome.Error(mapOf("second" to error)),
            RuleSelector.select(mapOf("first" to RuleResolution.Unique(plan), "second" to error)),
        )
    }

    @Test
    fun mapInsertionOrderDoesNotChangeAggregateOutcome() {
        val ambiguous = RuleResolution.Ambiguous(mapOf("target" to 2), listOf("two definitions"))
        val forward = RuleSelector.select(linkedMapOf("z" to ambiguous, "a" to RuleResolution.Missing()))
        val reverse = RuleSelector.select(linkedMapOf("a" to RuleResolution.Missing(), "z" to ambiguous))
        assertEquals(forward, reverse)
    }
}
