package io.github.nku100.impad.rules

import io.github.nku100.impad.compat.BuildIdentity
import io.github.nku100.impad.compat.code.CodeDelegation
import io.github.nku100.impad.compat.rules.CompletePlanSelection
import io.github.nku100.impad.compat.rules.HookPhase
import io.github.nku100.impad.compat.rules.RecognitionResult
import io.github.nku100.impad.compat.rules.VersionBinding
import java.util.concurrent.CancellationException
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertIs
import kotlin.test.assertTrue

class SemanticRuleResolverTest {
    @Test
    fun versionHintsChangeOrderButNeverExcludeConflictingRecognizer() = withSession { snapshot, session ->
        val calls = mutableListOf<String>()
        val queries = listOf(
            query("outside") { calls += "outside"; result(it, "second") },
            query("hinted") { calls += "hinted"; result(it, "first") },
        )
        val catalog = catalog(queries, listOf(VersionBinding("hinted", 100, 200), VersionBinding("outside", 1, 2)))
        val run = SemanticRuleResolver.resolveDetailed(identity(150), catalog, RecognitionContext(session, snapshot, catalog))
        assertEquals(listOf("hinted", "outside"), calls)
        assertEquals(calls, run.evaluatedRecognizerIds)
        assertIs<CompletePlanSelection.Conflict>(run.outcome)
        calls.clear()
        val reverse = SemanticRuleResolver.resolveDetailed(identity(1), catalog, RecognitionContext(session, snapshot, catalog))
        assertEquals(listOf("outside", "hinted"), calls)
        assertIs<CompletePlanSelection.Conflict>(reverse.outcome)
    }

    @Test
    fun noHintRecognizerStillRunsAndEquivalentExecutionsMerge() = withSession { snapshot, session ->
        val catalog = catalog(listOf(query("a") { result(it, "first") }, query("unhinted") { result(it, "first") }),
            listOf(VersionBinding("a", 100, null)))
        val selected = assertIs<CompletePlanSelection.Selected>(SemanticRuleResolver.resolveDetailed(identity(), catalog,
            RecognitionContext(session, snapshot, catalog)).outcome)
        assertEquals(setOf("a", "unhinted"), selected.match.evidenceByRecognizer.keys)
    }

    @Test
    fun partialMatchDoesNotBecomeParserErrorAndBudgetCannotBecomeSuccess() = withSession { snapshot, session ->
        val partial = catalog(listOf(query("partial") { RecognitionResult.NoMatch(listOf("login missing")) }, query("ok") { result(it, "first") }))
        assertIs<CompletePlanSelection.Selected>(SemanticRuleResolver.resolveDetailed(identity(), partial, RecognitionContext(session, snapshot, partial)).outcome)
        val budget = catalog(listOf(query("ok") { result(it, "first") }, query("budget") { throw AnalysisIncompleteException("graph budget") }))
        assertIs<CompletePlanSelection.Incomplete>(SemanticRuleResolver.resolveDetailed(identity(), budget, RecognitionContext(session, snapshot, budget)).outcome)
    }

    @Test
    fun aBrokenRecognizerIsNotHiddenBySuccess() = withSession { snapshot, session ->
        val catalog = catalog(listOf(query("ok") { result(it, "first") }, query("broken") { error("bad DEX") }))
        val error = assertIs<CompletePlanSelection.EvaluationError>(SemanticRuleResolver.resolveDetailed(identity(), catalog,
            RecognitionContext(session, snapshot, catalog)).outcome)
        assertTrue(error.diagnostics.getValue("broken").single().contains("bad DEX"))
    }

    @Test
    fun rejectsBadIdentityBeforeAnyQuery() = withSession { snapshot, session ->
        var queried = false
        val catalog = catalog(listOf(query("a") { queried = true; result(it, "first") }))
        assertFailsWith<IllegalArgumentException> { SemanticRuleResolver.resolveDetailed(identity().copy(packageName = "bad.package"),
            catalog, RecognitionContext(session, snapshot, catalog)) }
        assertEquals(false, queried)
    }

    @Test
    fun rejectsPlansWithChangedCallbackPhase() = withSession { snapshot, session ->
        val catalog = catalog(listOf(query("a") { context ->
            val plan = context.plan("strategy", mapOf("target" to context.session.findMethods(MethodQuery()).first()), listOf("proof")).plan
            val hook = plan.hooks.single()
            val changed = io.github.nku100.impad.compat.rules.PlanHook(hook.role, hook.descriptor, hook.shape, hook.source,
                HookPhase.AFTER, hook.configuration)
            RecognitionResult.Plans(listOf(io.github.nku100.impad.compat.rules.RecognizedPlan(
                io.github.nku100.impad.compat.rules.CompletePlan(plan.snapshotSha256, plan.contractId, plan.contractRevision,
                    plan.strategyId, plan.strategyRevision, listOf(changed)), emptyList())))
        }))
        assertIs<CompletePlanSelection.EvaluationError>(SemanticRuleResolver.resolveDetailed(identity(), catalog,
            RecognitionContext(session, snapshot, catalog)).outcome)
    }

    @Test
    fun forgedSnapshotAndDefinitionSourcesAreRejected() = withSession { snapshot, session ->
        for (variant in listOf("snapshot", "loader", "dexIndex", "dexHash", "descriptor", "shape")) {
            val catalog = catalog(listOf(query("a") { context ->
                val original = context.plan("strategy", mapOf("target" to context.session.findMethods(MethodQuery()).first()), listOf("proof")).plan
                val target = original.hooks.single()
                val source = when (variant) {
                    "loader" -> target.source.copy(loaderId = "unknown")
                    "dexIndex" -> target.source.copy(dexIndex = 42)
                    "dexHash" -> target.source.copy(dexSha256 = "b".repeat(64))
                    else -> target.source
                }
                val changed = io.github.nku100.impad.compat.rules.PlanHook(target.role,
                    if (variant == "descriptor") "Lsample/Target;->missing()Z" else target.descriptor,
                    if (variant == "shape") target.shape.copy(returnType = "int") else target.shape,
                    source, target.phase, target.configuration)
                RecognitionResult.Plans(listOf(io.github.nku100.impad.compat.rules.RecognizedPlan(
                    io.github.nku100.impad.compat.rules.CompletePlan(
                        if (variant == "snapshot") "b".repeat(64) else original.snapshotSha256,
                        original.contractId, original.contractRevision, original.strategyId, original.strategyRevision, listOf(changed)), emptyList())))
            }))
            assertIs<CompletePlanSelection.EvaluationError>(SemanticRuleResolver.resolveDetailed(identity(), catalog,
                RecognitionContext(session, snapshot, catalog)).outcome, variant)
        }
    }

    @Test
    fun cancellationMarksRemainingRecognizersIncompleteWithoutRunningThem() = withSession { snapshot, session ->
        var tailRan = false
        val catalog = catalog(listOf(query("cancel") { throw CancellationException("cancelled") },
            query("tail") { tailRan = true; result(it, "first") }))
        val run = SemanticRuleResolver.resolveDetailed(identity(), catalog, RecognitionContext(session, snapshot, catalog))
        assertIs<CompletePlanSelection.Incomplete>(run.outcome)
        assertEquals(setOf("cancel", "tail"), run.results.keys)
        assertEquals(listOf("cancel"), run.evaluatedRecognizerIds)
        assertEquals(false, tailRan)
    }

    @Test
    fun interruptedExceptionRestoresInterruptStatusAndNeverSelects() = withSession { snapshot, session ->
        val catalog = catalog(listOf(query("interrupt") { throw InterruptedException("stop") }))
        try {
            assertIs<CompletePlanSelection.Incomplete>(SemanticRuleResolver.resolveDetailed(identity(), catalog,
                RecognitionContext(session, snapshot, catalog)).outcome)
            assertTrue(Thread.currentThread().isInterrupted)
        } finally { Thread.interrupted() }
    }

    @Test
    fun aQueryThatSetsInterruptFlagCannotProduceSelected() = withSession { snapshot, session ->
        val catalog = catalog(listOf(query("a") { context ->
            result(context, "first").also { Thread.currentThread().interrupt() }
        }))
        try {
            assertIs<CompletePlanSelection.Incomplete>(SemanticRuleResolver.resolveDetailed(identity(), catalog,
                RecognitionContext(session, snapshot, catalog)).outcome)
        } finally { Thread.interrupted() }
    }

    @Test
    fun directoryFreezesRecognizerIdsBeforeEvaluation() = withSession { snapshot, session ->
        var id = "first"
        val mutable = object : SemanticRuleQuery {
            override val id: String get() = id
            override fun recognize(context: RecognitionContext) = result(context, "first")
        }
        val catalog = catalog(listOf(mutable, query("second") { result(it, "second") }))
        id = "second"
        val run = SemanticRuleResolver.resolveDetailed(identity(), catalog, RecognitionContext(session, snapshot, catalog))
        assertEquals(setOf("first", "second"), run.results.keys)
        assertIs<CompletePlanSelection.Conflict>(run.outcome)
    }

    @Test
    fun interruptDoesNotEraseARealEvaluationFailure() = withSession { snapshot, session ->
        val catalog = catalog(listOf(query("broken") {
            Thread.currentThread().interrupt()
            error("parser failed while cancelling")
        }))
        try {
            val outcome = assertIs<CompletePlanSelection.EvaluationError>(SemanticRuleResolver.resolveDetailed(identity(), catalog,
                RecognitionContext(session, snapshot, catalog)).outcome)
            assertTrue(outcome.diagnostics.getValue("broken").single().contains("parser failed"))
        } finally { Thread.interrupted() }
    }

    @Test
    fun recognitionSessionMustBelongToTheReadySnapshotAndNamespace() = withCodeDirectory { root ->
        val first = codeArchive(root, "first.apk", listOf("classes.dex" to "sample.Target"))
        val other = codeArchive(root, "other.apk", listOf("classes.dex" to "sample.Other"))
        val catalog = catalog(listOf(query("a") { RecognitionResult.NoMatch() }))
        StartupCodeSnapshot().capture("child", listOf(
            CodeLoaderInput("parent", null, CodeDelegation.PARENT_FIRST, listOf(first)),
            CodeLoaderInput("child", "parent", CodeDelegation.PARENT_FIRST, listOf(other)),
        )).use { snapshot ->
            StartupCodeSnapshot().capture("ready", listOf(CodeLoaderInput("ready", null, CodeDelegation.PARENT_FIRST, listOf(first)))).use { another ->
                EffectiveCodeRuleSession.open(snapshot) {
                    object : RuleSessionBackend {
                        override fun findMethods(query: MethodQuery) = emptyList<RuleMethod>()
                        override fun findClasses(typeName: String) = emptyList<RuleClass>()
                        override fun close() = Unit
                    }
                }.use { session ->
                    assertFailsWith<IllegalArgumentException> { RecognitionContext(session, another, catalog) }
                    session.inNamespace("parent").use { parent ->
                        assertFailsWith<IllegalArgumentException> { RecognitionContext(parent, snapshot, catalog) }
                    }
                }
            }
        }
    }

    @Test
    fun malformedDirectoryAndContextMismatchAreRejected() = withSession { snapshot, session ->
        val a = query("a") { result(it, "first") }
        assertFailsWith<IllegalArgumentException> { catalog(listOf(a, a)) }
        assertFailsWith<IllegalArgumentException> { catalog(listOf(a), listOf(VersionBinding("missing", 1, null))) }
        val catalog = catalog(listOf(a))
        val other = catalog(listOf(query("b") { result(it, "first") }))
        assertFailsWith<IllegalArgumentException> { SemanticRuleResolver.resolveDetailed(identity(), other, RecognitionContext(session, snapshot, catalog)) }
    }

    private fun result(context: RecognitionContext, name: String): RecognitionResult {
        val method = context.session.findMethods(MethodQuery()).single { it.descriptor.contains("->$name(") }
        return RecognitionResult.Plans(listOf(context.plan("strategy", mapOf("target" to method), listOf("complete relation $name"))))
    }

    private fun query(name: String, block: (RecognitionContext) -> RecognitionResult) = object : SemanticRuleQuery {
        override val id = name
        override fun recognize(context: RecognitionContext) = block(context)
    }

    private fun catalog(queries: List<SemanticRuleQuery>, hints: List<VersionBinding> = emptyList()) = SemanticRuleCatalog(
        "qq", "qq_tablet", 1, setOf("target"), queries,
        listOf(RuleStrategyContract("strategy", 1, listOf(HookRoleContract("target", HookPhase.BEFORE, mapOf("enum" to "TABLET"))))), hints,
    )

    private fun identity(version: Long = 150) = BuildIdentity("com.tencent.mobileqq", "test", version, "arm64-v8a", "a".repeat(64),
        "ea6e97ad6c34f7039a9c6daba732c97d0e098e83ede2b4d52c76eb0184ac7a38")

    private fun withSession(block: (CapturedCodeSnapshot, RuleSession) -> Unit) = withCodeDirectory { root ->
        val archive = codeArchive(root, "input.apk", listOf("classes.dex" to "sample.Target"))
        StartupCodeSnapshot().capture("ready", listOf(CodeLoaderInput("ready", null, CodeDelegation.PARENT_FIRST, listOf(archive)))).use { snapshot ->
            EffectiveCodeRuleSession.open(snapshot) {
                object : RuleSessionBackend {
                    override fun findMethods(query: MethodQuery) = listOf("first", "second").map { name ->
                        RuleMethod("Lsample/Target;->$name()Z", "sample.Target", emptyList(), "boolean", true,
                            2, emptySet(), emptyList(), emptyList())
                    }.filter { query.declaredClass == null || it.className == query.declaredClass }
                    override fun findClasses(typeName: String) = emptyList<RuleClass>()
                    override fun close() = Unit
                }
            }.use { block(snapshot, it) }
        }
    }
}
