package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.compat.rules.VersionRouter
import io.github.nku100.impad.rules.RuleCatalog
import io.github.nku100.impad.rules.RuleSession
import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

class WeChatTabletLoginRule002Test {
    @Test
    fun candidateRuleIsAdditiveAndRoutesOnlyForwardFromVersion3200() {
        val catalog = RuleCatalog.forApp("wechat")
        val historical = assertNotNull(catalog.definitions.singleOrNull { it.ruleId == HISTORICAL_RULE_ID })
        val future = assertNotNull(catalog.definitions.singleOrNull { it.ruleId == RULE_ID })
        assertEquals(HISTORICAL_STRATEGY_ID, historical.strategyId)
        assertEquals(setOf("tablet", "login"), historical.requiredTargetIds)
        assertEquals(STRATEGY_ID, future.strategyId)
        assertEquals(setOf("tablet", "login"), future.requiredTargetIds)
        assertTrue(STRATEGY_ID in catalog.strategyIds)

        assertEquals(3040L, catalog.bindings.single { it.ruleId == HISTORICAL_RULE_ID }.min)
        assertEquals(3040L, catalog.bindings.single { it.ruleId == HISTORICAL_RULE_ID }.max)
        assertEquals(3200L, catalog.bindings.single { it.ruleId == RULE_ID }.min)
        assertEquals(null, catalog.bindings.single { it.ruleId == RULE_ID }.max)
        assertEquals(listOf(HISTORICAL_RULE_ID), VersionRouter.candidates(3039L, catalog.bindings))
        assertEquals(listOf(HISTORICAL_RULE_ID), VersionRouter.candidates(3040L, catalog.bindings))
        assertEquals(listOf(HISTORICAL_RULE_ID, RULE_ID), VersionRouter.candidates(3041L, catalog.bindings))
        assertEquals(listOf(HISTORICAL_RULE_ID, RULE_ID), VersionRouter.candidates(3199L, catalog.bindings))
        assertEquals(listOf(RULE_ID), VersionRouter.candidates(3200L, catalog.bindings))
        assertEquals(listOf(RULE_ID), VersionRouter.candidates(3201L, catalog.bindings))
    }

    @Test
    fun resolvesTheAggregateTabletDecisionAndItsLinkedLoginSetup() = withApk(WeChatForwardRuleDexFixture.apk()) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val selected = assertIs<RuleResolution.Unique>(WeChatTabletLoginRule002.resolve(session))
            assertEquals(mapOf("tablet" to 1, "login" to 1), selected.candidateCounts)
            assertEquals(RULE_ID, selected.plan.rule.ruleId)
            assertEquals(STRATEGY_ID, selected.plan.rule.strategyId)
            assertEquals(setOf("tablet", "login"), selected.plan.targets.keys)
            assertTrue(selected.plan.targets.getValue("tablet").shape.isStatic)
            assertTrue(selected.plan.targets.getValue("login").shape.isStatic)
        }
    }

    @Test
    fun missingTabletModelSemanticsKeepTheCompleteRuleClosed() = withApk(
        WeChatForwardRuleDexFixture.apk(WeChatForwardRuleDexFixture.Options(tabletModelMarkers = false)),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val missing = assertIs<RuleResolution.Missing>(WeChatTabletLoginRule002.resolve(session))
            assertEquals(mapOf("tablet" to 0, "login" to 0), missing.candidateCounts)
        }
    }

    @Test
    fun multipleSemanticallyValidTabletDecisionsRemainAmbiguous() = withApk(
        WeChatForwardRuleDexFixture.apk(
            WeChatForwardRuleDexFixture.Options(
                tabletOwners = listOf("Lfixture/TabletDecision;", "Lfixture/TabletDecisionCopy;"),
            ),
        ),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val ambiguous = assertIs<RuleResolution.Ambiguous>(WeChatTabletLoginRule002.resolve(session))
            assertEquals(mapOf("tablet" to 2, "login" to 0), ambiguous.candidateCounts)
        }
    }

    @Test
    fun multipleLinkedLoginSetupsRemainAmbiguous() = withApk(
        WeChatForwardRuleDexFixture.apk(
            WeChatForwardRuleDexFixture.Options(
                loginOwners = listOf("Lfixture/LoginSetup;", "Lfixture/LoginSetupCopy;"),
            ),
        ),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val ambiguous = assertIs<RuleResolution.Ambiguous>(WeChatTabletLoginRule002.resolve(session))
            assertEquals(mapOf("tablet" to 1, "login" to 2), ambiguous.candidateCounts)
        }
    }

    @Test
    fun modelAndWidthLookalikesWithoutTrueReturningModelGateAreRejected() = withApk(
        WeChatForwardRuleDexFixture.apk(
            WeChatForwardRuleDexFixture.Options(modelGateControlsDecision = false),
        ),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val missing = assertIs<RuleResolution.Missing>(WeChatTabletLoginRule002.resolve(session))
            assertEquals(mapOf("tablet" to 0, "login" to 0), missing.candidateCounts)
        }
    }

    @Test
    fun aWrongWidthThresholdCannotSelectTheTabletDecision() = withApk(
        WeChatForwardRuleDexFixture.apk(WeChatForwardRuleDexFixture.Options(widthThreshold = 601)),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val missing = assertIs<RuleResolution.Missing>(WeChatTabletLoginRule002.resolve(session))
            assertEquals(mapOf("tablet" to 0, "login" to 0), missing.candidateCounts)
        }
    }

    @Test
    fun aLoginMethodWithoutTheTabletWrapperCallIsNotLinked() = withApk(
        WeChatForwardRuleDexFixture.apk(
            WeChatForwardRuleDexFixture.Options(loginCallsTabletHelper = false),
        ),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val missing = assertIs<RuleResolution.Missing>(WeChatTabletLoginRule002.resolve(session))
            assertEquals(mapOf("tablet" to 1, "login" to 0), missing.candidateCounts)
        }
    }

    private fun withApk(apk: File, block: (File) -> Unit) {
        try {
            loadLockedLinuxNative()
            block(apk)
        } finally {
            apk.delete()
        }
    }

    @Test
    fun discardedTabletResultsCannotLinkTheLoginPath() {
        for (options in listOf(
            WeChatForwardRuleDexFixture.Options(wrapperForwardsResult = false),
            WeChatForwardRuleDexFixture.Options(loginBooleanControlsVisibility = false),
        )) {
            withApk(WeChatForwardRuleDexFixture.apk(options)) { apk ->
                RuleSession.open(listOf(apk)).use { session ->
                    val missing = assertIs<RuleResolution.Missing>(WeChatTabletLoginRule002.resolve(session))
                    assertEquals(mapOf("tablet" to 1, "login" to 0), missing.candidateCounts)
                }
            }
        }
    }

    private fun loadLockedLinuxNative() {
        if (System.getProperty("impad.dexkit.native.loaded") == "true") return
        check(System.getProperty("os.name").contains("linux", ignoreCase = true)) {
            "Real DEX fixture tests require the task-locked Linux DexKit native"
        }
        val runtimeDirectory = File(checkNotNull(System.getenv("DEXKIT_RUNTIME_DIR")) {
            "Gradle must pass its prepared DexKit output directory to the real DEX tests"
        }).canonicalFile
        Class.forName("org.luckypray.dexkit.DexKitBridge")
        io.github.nku100.impad.rules.DexKitRuntimeLoader.load(runtimeDirectory)
        System.setProperty("impad.dexkit.native.loaded", "true")
    }

    private companion object {
        const val HISTORICAL_RULE_ID = "wechat_tablet_login_001"
        const val HISTORICAL_STRATEGY_ID = "wechat_tablet_login_behavior_001"
        const val RULE_ID = "wechat_tablet_login_002"
        const val STRATEGY_ID = "wechat_tablet_login_behavior_002"
    }
}
