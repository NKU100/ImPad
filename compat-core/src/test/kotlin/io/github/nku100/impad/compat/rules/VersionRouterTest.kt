package io.github.nku100.impad.compat.rules

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class VersionRouterTest {
    private val bindings = listOf(
        VersionBinding("A", 100, 180),
        VersionBinding("B", 220, null),
    )

    @Test
    fun routesInclusiveEndpointsGapsAndVersionsBelowTheFirstBinding() {
        assertEquals(listOf("A"), VersionRouter.candidates(100, bindings))
        assertEquals(listOf("A"), VersionRouter.candidates(180, bindings))
        assertEquals(listOf("A", "B"), VersionRouter.candidates(181, bindings))
        assertEquals(listOf("A", "B"), VersionRouter.candidates(219, bindings))
        assertEquals(listOf("B"), VersionRouter.candidates(220, bindings))
        assertEquals(listOf("B"), VersionRouter.candidates(Long.MAX_VALUE, bindings))
        assertEquals(listOf("A"), VersionRouter.candidates(99, bindings))
    }

    @Test
    fun routingDoesNotDependOnBindingInputOrderAndRejectsNonpositiveVersionCodes() {
        assertEquals(
            VersionRouter.candidates(200, bindings),
            VersionRouter.candidates(200, bindings.reversed()),
        )
        assertFailsWith<IllegalArgumentException> { VersionRouter.candidates(0, bindings) }
    }

    @Test
    fun candidatesRejectsSingleFiniteBinding() {
        assertFailsWith<IllegalArgumentException> {
            VersionRouter.candidates(100, listOf(VersionBinding("A", 100, 180)))
        }
    }

    @Test
    fun candidatesRejectsMultipleFiniteBindings() {
        assertFailsWith<IllegalArgumentException> {
            VersionRouter.candidates(200, listOf(
                VersionBinding("A", 100, 180),
                VersionBinding("B", 220, 260),
            ))
        }
    }

    @Test
    fun candidatesRejectsFiniteLongMaxValueTail() {
        assertFailsWith<IllegalArgumentException> {
            VersionRouter.candidates(Long.MAX_VALUE, listOf(VersionBinding("A", 100, Long.MAX_VALUE)))
        }
    }

    @Test
    fun validateRejectsSingleFiniteBinding() {
        assertFailsWith<IllegalArgumentException> {
            VersionRouter.validate(
                listOf(VersionBinding("A", 100, 180)),
                listOf(RuleDefinition("A", "strategy", setOf("target"))),
                setOf("strategy"),
            )
        }
    }

    @Test
    fun validateRejectsMultipleFiniteBindings() {
        val finiteBindings = listOf(VersionBinding("A", 100, 180), VersionBinding("B", 220, 260))
        assertFailsWith<IllegalArgumentException> {
            VersionRouter.validate(
                finiteBindings,
                finiteBindings.map { RuleDefinition(it.ruleId, "strategy", setOf("target")) },
                setOf("strategy"),
            )
        }
    }

    @Test
    fun validateRejectsFiniteLongMaxValueTail() {
        assertFailsWith<IllegalArgumentException> {
            VersionRouter.validate(
                listOf(VersionBinding("A", 100, Long.MAX_VALUE)),
                listOf(RuleDefinition("A", "strategy", setOf("target"))),
                setOf("strategy"),
            )
        }
    }

    @Test
    fun oneRuleAcrossNeighboringBindingsAppearsOnlyOnceInGapCandidates() {
        val repeatedRuleBindings = listOf(
            VersionBinding("A", 100, 180),
            VersionBinding("A", 220, null),
        )
        assertEquals(listOf("A"), VersionRouter.candidates(200, repeatedRuleBindings))
    }

    @Test
    fun validatesEmptyAndOpenEndedTablesAndRoutesLongMaxValue() {
        VersionRouter.validate(emptyList(), emptyList(), emptySet())
        VersionRouter.validate(bindings, listOf(
            RuleDefinition("A", "strategyA", setOf("target")),
            RuleDefinition("B", "strategyB", setOf("target")),
        ), setOf("strategyA", "strategyB"))
        assertEquals(emptyList(), VersionRouter.candidates(100, emptyList()))
        assertEquals(listOf("B"), VersionRouter.candidates(Long.MAX_VALUE, bindings))
    }

    @Test
    fun rejectsInvalidBoundsOverlapsDuplicateDeclarationsAndUnknownReferences() {
        assertFailsWith<IllegalArgumentException> {
            VersionRouter.candidates(1, listOf(VersionBinding("A", 0, null)))
        }
        assertFailsWith<IllegalArgumentException> {
            VersionRouter.candidates(1, listOf(VersionBinding("A", 9, 8)))
        }
        assertFailsWith<IllegalArgumentException> {
            VersionRouter.candidates(1, listOf(VersionBinding("A", 1, null), VersionBinding("B", 3, 4)))
        }
        assertFailsWith<IllegalArgumentException> {
            VersionRouter.candidates(1, listOf(VersionBinding("A", 1, 4), VersionBinding("B", 4, null)))
        }
        assertFailsWith<IllegalArgumentException> {
            VersionRouter.candidates(1, listOf(VersionBinding("A", 1, 3), VersionBinding("B", 1, 3)))
        }
        assertFailsWith<IllegalArgumentException> {
            VersionRouter.validate(
                listOf(VersionBinding("unknown", 1, null)),
                listOf(RuleDefinition("known", "strategy", setOf("target"))),
                setOf("strategy"),
            )
        }
    }

    @Test
    fun validatesRuleAndStrategyDeclarationsAndAllowsAnEmptyRuleTable() {
        val rule = RuleDefinition("A", "strategy", setOf("target"))
        assertFailsWith<IllegalArgumentException> {
            VersionRouter.validate(listOf(VersionBinding("A", 1, null)), listOf(rule, rule), setOf("strategy"))
        }
        assertFailsWith<IllegalArgumentException> {
            VersionRouter.validate(listOf(VersionBinding("A", 1, null)), listOf(rule), emptySet())
        }
        assertFailsWith<IllegalArgumentException> {
            VersionRouter.validate(
                listOf(VersionBinding("A", 1, null)),
                listOf(rule.copy(strategyId = "missing")),
                setOf("strategy"),
            )
        }
        assertFailsWith<IllegalArgumentException> {
            VersionRouter.validate(
                listOf(VersionBinding("A", 1, null)),
                listOf(rule.copy(requiredTargetIds = emptySet())),
                setOf("strategy"),
            )
        }
        assertEquals(emptyList(), VersionRouter.candidates(100, emptyList()))
    }
}
