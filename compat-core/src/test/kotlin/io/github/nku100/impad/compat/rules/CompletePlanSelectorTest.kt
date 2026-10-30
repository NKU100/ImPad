package io.github.nku100.impad.compat.rules

import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertIs
import kotlin.test.assertNotEquals

class CompletePlanSelectorTest {
    @Test
    fun identicalExecutionMergesDifferentRecognizersAndPreservesAllEvidence() {
        val plan = plan()
        val selected = assertIs<CompletePlanSelection.Selected>(CompletePlanSelector.select(linkedMapOf(
            "second" to plans(plan, "second proof"), "first" to plans(plan(), "first proof"),
        )))
        assertEquals(plan.executionSha256(), selected.match.plan.executionSha256())
        assertEquals(mapOf("first" to listOf("first proof"), "second" to listOf("second proof")), selected.match.evidenceByRecognizer)
    }

    @Test
    fun everyExecutionFieldDifferencePreventsDeduplication() {
        val baseline = plan()
        val variants = listOf(
            plan(snapshot = "b".repeat(64)), plan(contract = "other"), plan(contractRevision = 2),
            plan(strategy = "another"), plan(strategyRevision = 2),
            plan(hook = hook(role = "other")), plan(hook = hook(descriptor = "Lsample/Target;->other()Z")),
            plan(hook = hook(shape = MethodShape(listOf("int"), "boolean", true))),
            plan(hook = hook(shape = MethodShape(emptyList(), "int", true))),
            plan(hook = hook(shape = MethodShape(emptyList(), "boolean", false))),
            plan(hook = hook(source = TargetCodeSource("parent", 0, "a".repeat(64)))),
            plan(hook = hook(source = TargetCodeSource("ready", 1, "a".repeat(64)))),
            plan(hook = hook(source = TargetCodeSource("ready", 0, "b".repeat(64)))),
            plan(hook = hook(phase = HookPhase.AFTER)), plan(hook = hook(configuration = mapOf("result" to "false"))),
        )
        variants.forEach { variant ->
            assertNotEquals(baseline.executionSha256(), variant.executionSha256())
            assertIs<CompletePlanSelection.Conflict>(CompletePlanSelector.select(mapOf("a" to plans(baseline), "b" to plans(variant))))
        }
    }

    @Test
    fun roleOrderMattersButMapInsertionOrderAndHashCaseDoNot() {
        val first = hook(configuration = linkedMapOf("result" to "true", "argument" to "0"))
        val same = hook(configuration = linkedMapOf("argument" to "0", "result" to "true"),
            source = TargetCodeSource("ready", 0, "A".repeat(64)))
        assertEquals(plan(hook = first), plan(hook = same))
        val second = hook(role = "login", descriptor = "Lsample/Target;->login()V")
        assertNotEquals(plan(hooks = listOf(first, second)), plan(hooks = listOf(second, first)))
    }

    @Test
    fun oneSuccessCannotHideIncompleteUnsupportedOrError() {
        val success = plans(plan())
        assertIs<CompletePlanSelection.Incomplete>(CompletePlanSelector.select(mapOf("ok" to success,
            "truncated" to RecognitionResult.Incomplete("graph budget exhausted", listOf(RecognizedPlan(plan(), listOf("found")))))))
        assertIs<CompletePlanSelection.UnsupportedInput>(CompletePlanSelector.select(mapOf("ok" to success,
            "unknown" to RecognitionResult.UnsupportedInput(listOf("unknown input")))))
        assertIs<CompletePlanSelection.EvaluationError>(CompletePlanSelector.select(mapOf("ok" to success,
            "broken" to RecognitionResult.EvaluationError(listOf("parser failure")))))
    }

    @Test
    fun twoProvedDifferentPlansCanReportConflictBeforeEnumerationFinishes() {
        val result = CompletePlanSelector.select(mapOf("unfinished" to RecognitionResult.Incomplete("budget",
            listOf(RecognizedPlan(plan(), emptyList()), RecognizedPlan(plan(strategy = "different"), emptyList())))))
        assertEquals(2, assertIs<CompletePlanSelection.Conflict>(result).plans.size)
    }

    @Test
    fun partialEvidenceWithoutCompletePlanDoesNotBlockAnotherSuccess() {
        assertIs<CompletePlanSelection.Selected>(CompletePlanSelector.select(mapOf(
            "partial" to RecognitionResult.NoMatch(listOf("tablet found but login missing")), "complete" to plans(plan()),
        )))
        assertIs<CompletePlanSelection.NoMatch>(CompletePlanSelector.select(emptyMap()))
        assertIs<CompletePlanSelection.NoMatch>(CompletePlanSelector.select(mapOf("partial" to RecognitionResult.NoMatch())))
    }

    @Test
    fun resultOrderDoesNotChangeOutcomeOrMergedEvidence() {
        val a = "a" to plans(plan(), "one")
        val b = "b" to plans(plan(strategy = "different"), "two")
        val forward = assertIs<CompletePlanSelection.Conflict>(CompletePlanSelector.select(linkedMapOf(a, b)))
        val reverse = assertIs<CompletePlanSelection.Conflict>(CompletePlanSelector.select(linkedMapOf(b, a)))
        assertEquals(forward.plans.map { it.plan.executionSha256() }, reverse.plans.map { it.plan.executionSha256() })
    }

    @Test
    fun executionIsFrozenAgainstCallerCollectionMutationAndSurvivesJsonRoundTrip() {
        val parameters = mutableListOf("int")
        val configuration = mutableMapOf("result" to "true")
        val hooks = mutableListOf(hook(shape = MethodShape(parameters, "boolean", true), configuration = configuration))
        val plan = plan(hooks = hooks)
        val before = plan.executionSha256()
        parameters.clear(); configuration.clear(); hooks.clear()
        assertEquals(before, plan.executionSha256())
        assertEquals(listOf("int"), plan.hooks.single().shape.parameters)
        assertEquals(mapOf("result" to "true"), plan.hooks.single().configuration)
        assertFailsWith<UnsupportedOperationException> { (plan.hooks as MutableList).clear() }
        val json = Json.encodeToString(plan)
        assertEquals(plan, Json.decodeFromString<CompletePlan>(json))
    }

    @Test
    fun selectedEvidenceIsCopiedAndCannotBeChangedAfterSelection() {
        val evidence = mutableListOf("original proof")
        val selected = assertIs<CompletePlanSelection.Selected>(CompletePlanSelector.select(mapOf("a" to
            RecognitionResult.Plans(listOf(RecognizedPlan(plan(), evidence))))))
        evidence.clear()
        assertEquals(listOf("original proof"), selected.match.evidenceByRecognizer.getValue("a"))
    }

    @Test
    fun recognitionCompletionAndEvidenceAreFrozenBeforeOtherRecognizersRun() {
        val evidence = mutableListOf("original")
        val plans = mutableListOf(RecognizedPlan(plan(), evidence))
        val complete = RecognitionResult.Plans(plans)
        val incomplete = RecognitionResult.Incomplete("budget", plans)
        val diagnostics = mutableListOf("partial relation")
        val missing = RecognitionResult.NoMatch(diagnostics)
        plans.clear(); evidence.clear(); diagnostics.clear()
        assertEquals(1, complete.plans.size)
        assertEquals(listOf("original"), complete.plans.single().evidence)
        assertEquals(1, incomplete.discoveredPlans.size)
        assertEquals(listOf("partial relation"), missing.diagnostics)
        assertIs<CompletePlanSelection.Selected>(CompletePlanSelector.select(mapOf("a" to complete)))
    }

    @Test
    fun malformedOrPartialExecutionPlansAreRejected() {
        assertFailsWith<IllegalArgumentException> { plan(snapshot = "invalid") }
        assertFailsWith<IllegalArgumentException> { plan(contractRevision = 0) }
        assertFailsWith<IllegalArgumentException> { plan(strategyRevision = 0) }
        assertFailsWith<IllegalArgumentException> { plan(hooks = emptyList()) }
        assertFailsWith<IllegalArgumentException> { plan(hooks = listOf(hook(), hook())) }
        assertFailsWith<IllegalArgumentException> { TargetCodeSource("ready", -1, "a".repeat(64)) }
        assertFailsWith<IllegalArgumentException> { RecognitionResult.Plans(emptyList()) }
    }

    private fun plans(plan: CompletePlan, evidence: String = "proof") = RecognitionResult.Plans(listOf(RecognizedPlan(plan, listOf(evidence))))
    private fun plan(snapshot: String = "a".repeat(64), contract: String = "tablet", contractRevision: Int = 1,
        strategy: String = "tablet_callback", strategyRevision: Int = 1, hook: PlanHook = hook(), hooks: List<PlanHook> = listOf(hook)) =
        CompletePlan(snapshot, contract, contractRevision, strategy, strategyRevision, hooks)
    private fun hook(role: String = "tablet", descriptor: String = "Lsample/Target;->tablet()Z",
        shape: MethodShape = MethodShape(emptyList(), "boolean", true),
        source: TargetCodeSource = TargetCodeSource("ready", 0, "a".repeat(64)), phase: HookPhase = HookPhase.BEFORE,
        configuration: Map<String, String> = mapOf("result" to "true")) = PlanHook(role, descriptor, shape, source, phase, configuration)
}
