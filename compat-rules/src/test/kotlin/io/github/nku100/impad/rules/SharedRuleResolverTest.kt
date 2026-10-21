package io.github.nku100.impad.rules

import io.github.nku100.impad.compat.BuildIdentity
import io.github.nku100.impad.compat.rules.MethodShape
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.ResolvedTarget
import io.github.nku100.impad.compat.rules.RuleDefinition
import io.github.nku100.impad.compat.rules.RuleRef
import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.compat.rules.SelectionOutcome
import io.github.nku100.impad.compat.rules.VersionBinding
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

class SharedRuleResolverTest {
    @Test
    fun routesAndRunsCandidateRulesInDeclarationOrderWithinOneSession() {
        val calls = mutableListOf<String>()
        val catalog = catalog(
            queries = listOf(
                query("qq_a") { calls += "qq_a"; missing() },
                query("qq_b") { calls += "qq_b"; unique("qq_b") },
            ),
            bindings = listOf(VersionBinding("qq_a", 100, 180), VersionBinding("qq_b", 220, null)),
        )

        val outcome = SharedRuleResolver.resolve(identity(versionCode = 181), catalog, EmptyRuleSession)

        assertEquals(listOf("qq_a", "qq_b"), calls)
        assertEquals("qq_b", assertIs<SelectionOutcome.Selected>(outcome).plan.rule.ruleId)
    }

    @Test
    fun rejectsUntrustedIdentityBeforeAnyRuleQuery() {
        var queried = false
        val catalog = catalog(listOf(query("qq_a") { queried = true; unique("qq_a") }))

        assertFailsWith<IllegalArgumentException> {
            SharedRuleResolver.resolve(identity(packageName = "com.example.other"), catalog, EmptyRuleSession)
        }

        assertEquals(false, queried)
    }

    @Test
    fun rejectsUniqueResultsThatDoNotMatchTheImmutableRuleContract() {
        val query = query("qq_a") {
            RuleResolution.Unique(
                ResolvedPlan(
                    RuleRef("another_rule", "qq_tablet_001"),
                    mapOf("device_type" to ResolvedTarget("Lx;->a()V", MethodShape(emptyList(), "void", true))),
                ),
                candidateCounts = mapOf("device_type" to 1),
            )
        }

        assertIs<SelectionOutcome.Error>(
            SharedRuleResolver.resolve(identity(), catalog(listOf(query)), EmptyRuleSession),
        )
    }

    @Test
    fun convertsNonFatalQueryFailureToRuleErrorWithUnavailableCounts() {
        val outcome = SharedRuleResolver.resolve(
            identity(),
            catalog(listOf(query("qq_a") { throw IllegalStateException("DexKit query exploded") })),
            EmptyRuleSession,
        )

        val error = assertIs<SelectionOutcome.Error>(outcome).rules.getValue("qq_a")
        assertEquals(emptyMap(), error.candidateCounts)
        assertTrue(error.diagnostics.single().contains("qq_a"))
        assertTrue(error.diagnostics.single().contains("candidate counts unavailable"))
        assertTrue(error.diagnostics.single().contains("DexKit query exploded"))
    }

    private fun catalog(
        queries: List<RuleQuery>,
        bindings: List<VersionBinding> = listOf(VersionBinding("qq_a", 100, null)),
    ) = RuleCatalog(
        appId = "qq",
        queries = queries,
        bindings = bindings,
        strategyIds = setOf("qq_tablet_001"),
    )

    private fun query(ruleId: String, resolver: (RuleSession) -> RuleResolution) = object : RuleQuery {
        override val definition = RuleDefinition(ruleId, "qq_tablet_001", setOf("device_type"))
        override fun resolve(session: RuleSession): RuleResolution = resolver(session)
    }

    private fun unique(ruleId: String) = RuleResolution.Unique(
        ResolvedPlan(
            RuleRef(ruleId, "qq_tablet_001"),
            mapOf("device_type" to ResolvedTarget("Lx;->a()V", MethodShape(emptyList(), "void", true))),
        ),
        candidateCounts = mapOf("device_type" to 1),
    )

    private fun missing() = RuleResolution.Missing(candidateCounts = mapOf("device_type" to 0))

    private fun identity(
        packageName: String = "com.tencent.mobileqq",
        versionCode: Long = 14378,
    ) = BuildIdentity(
        packageName = packageName,
        versionName = "9.3.1",
        versionCode = versionCode,
        abi = "arm64-v8a",
        apkSha256 = "a".repeat(64),
        signerSha256 = "ea6e97ad6c34f7039a9c6daba732c97d0e098e83ede2b4d52c76eb0184ac7a38",
    )

    private object EmptyRuleSession : RuleSession {
        override fun findMethods(query: MethodQuery): List<RuleMethod> = emptyList()
        override fun findClasses(typeName: String): List<RuleClass> = emptyList()
        override fun close() = Unit
    }
}
