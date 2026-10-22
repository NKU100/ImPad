package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.compat.rules.RuleDefinition
import io.github.nku100.impad.compat.rules.VersionBinding
import io.github.nku100.impad.compat.rules.VersionRouter
import io.github.nku100.impad.rules.RuleCatalog
import io.github.nku100.impad.rules.RuleQuery
import io.github.nku100.impad.rules.RuleSession
import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertIs
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

class WeChatTabletLoginRuleTest {
    @Test
    fun catalogPreservesTheHistorical3040TabletLoginRoute() {
        val catalog = runCatching { RuleCatalog.forApp("wechat") }.getOrNull()
        assertNotNull(catalog, "WeChat rule catalog route is missing")
        assertHistoricalCatalog(catalog)
    }

    @Test
    fun syntheticCatalogAllowsAForwardOnlyRuleExtensionAtAnyFutureVersion() {
        val extended = syntheticCatalog(
            definitions = listOf(historicalDefinition(), futureDefinition()),
            bindings = listOf(
                VersionBinding(HISTORICAL_RULE_ID, 3040L, 3040L),
                VersionBinding(FUTURE_RULE_ID, FUTURE_VERSION, null),
            ),
            strategies = setOf(HISTORICAL_STRATEGY_ID, FUTURE_STRATEGY_ID),
        )

        assertHistoricalCatalog(extended)
        assertEquals(listOf(HISTORICAL_RULE_ID), VersionRouter.candidates(3040L, extended.bindings))
        assertEquals(listOf(FUTURE_RULE_ID), VersionRouter.candidates(FUTURE_VERSION, extended.bindings))
    }

    @Test
    fun syntheticCatalogRejectsReplacingTheHistoricalRoute() {
        val replaced = syntheticCatalog(
            definitions = listOf(historicalDefinition(), futureDefinition()),
            bindings = listOf(VersionBinding(FUTURE_RULE_ID, 3040L, null)),
            strategies = setOf(HISTORICAL_STRATEGY_ID, FUTURE_STRATEGY_ID),
        )

        assertFailsWith<AssertionError> {
            assertHistoricalCatalog(replaced)
        }
    }

    @Test
    fun syntheticCatalogRejectsChangingHistoricalTargetsOrStrategy() {
        val changedTargets = syntheticCatalog(
            definitions = listOf(
                historicalDefinition(targets = setOf("tablet")),
                futureDefinition(),
            ),
            bindings = forwardBindings(),
            strategies = setOf(HISTORICAL_STRATEGY_ID, FUTURE_STRATEGY_ID),
        )
        val changedStrategy = syntheticCatalog(
            definitions = listOf(
                historicalDefinition(strategyId = "wechat_tablet_login_behavior_replaced"),
                futureDefinition(),
            ),
            bindings = forwardBindings(),
            strategies = setOf(
                HISTORICAL_STRATEGY_ID,
                "wechat_tablet_login_behavior_replaced",
                FUTURE_STRATEGY_ID,
            ),
        )

        assertFailsWith<AssertionError> {
            assertHistoricalCatalog(changedTargets)
        }
        assertFailsWith<AssertionError> {
            assertHistoricalCatalog(changedStrategy)
        }
    }

    @Test
    fun resolvesBothTargetsOnlyWhenTheirDexSemanticsAndCallRelationshipMatch() = withApk(WeChatRuleDexFixture.apk()) { apk ->
        val catalog = RuleCatalog.forApp("wechat")
        RuleSession.open(listOf(apk)).use { session ->
            val selected = assertIs<RuleResolution.Unique>(rule().resolve(session))
            assertEquals(mapOf("tablet" to 1, "login" to 1), selected.candidateCounts)
            assertEquals(catalog.definitions.single { it.ruleId == HISTORICAL_RULE_ID }.ref, selected.plan.rule)
            assertEquals(setOf("tablet", "login"), selected.plan.targets.keys)
        }
    }

    @Test
    fun aMissingTabletDefinitionFailsTheCompleteRuleClosed() = withApk(
        WeChatRuleDexFixture.apk(tabletSemanticAnchor = false),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val missing = assertIs<RuleResolution.Missing>(rule().resolve(session))
            assertEquals(mapOf("tablet" to 0, "login" to 0), missing.candidateCounts)
        }
    }

    @Test
    fun multipleTabletDefinitionsRemainAmbiguous() = withApk(
        WeChatRuleDexFixture.apk(tabletOwners = listOf("Lfixture/TabletGate;", "Lfixture/TabletGateCopy;")),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val ambiguous = assertIs<RuleResolution.Ambiguous>(rule().resolve(session))
            assertEquals(2, ambiguous.candidateCounts["tablet"])
        }
    }

    @Test
    fun duplicateTabletDefinitionAcrossDexFilesRemainsAmbiguous() = withApk(
        WeChatRuleDexFixture.apk(duplicateTabletAcrossDex = true),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val ambiguous = assertIs<RuleResolution.Ambiguous>(rule().resolve(session))
            assertEquals(2, ambiguous.candidateCounts["tablet"])
        }
    }

    @Test
    fun multipleLoginDefinitionsRemainAmbiguous() = withApk(
        WeChatRuleDexFixture.apk(loginOwners = listOf("Lfixture/LoginBinding;", "Lfixture/LoginBindingCopy;")),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val ambiguous = assertIs<RuleResolution.Ambiguous>(rule().resolve(session))
            assertEquals(2, ambiguous.candidateCounts["login"])
        }
    }

    @Test
    fun aLoginMethodWithoutTheTabletGateCallIsAFalsePositive() = withApk(
        WeChatRuleDexFixture.apk(loginCallsTabletGate = false),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val missing = assertIs<RuleResolution.Missing>(rule().resolve(session))
            assertEquals(mapOf("tablet" to 1, "login" to 0), missing.candidateCounts)
        }
    }

    @Test
    fun shapeAndAnchorLookalikesWithoutTheTabletModelAndDisplaySemanticsAreRejected() = withApk(
        WeChatRuleDexFixture.apk(tabletReadsModel = false, tabletUsesDisplayMetrics = false),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val missing = assertIs<RuleResolution.Missing>(rule().resolve(session))
            assertEquals(mapOf("tablet" to 0, "login" to 0), missing.candidateCounts)
        }
    }

    @Test
    fun aWidthComparisonUsingTheWrongThresholdIsRejected() = withApk(
        WeChatRuleDexFixture.apk(tabletWidthThreshold = 601),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val missing = assertIs<RuleResolution.Missing>(rule().resolve(session))
            assertEquals(mapOf("tablet" to 0, "login" to 0), missing.candidateCounts)
        }
    }

    @Test
    fun aWidthGetterInvokedThroughItsInterfaceIsAccepted() = withApk(
        WeChatRuleDexFixture.apk(tabletGetterOpcode = com.android.tools.smali.dexlib2.Opcode.INVOKE_INTERFACE),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            assertIs<RuleResolution.Unique>(rule().resolve(session))
        }
    }

    @Test
    fun aWidthComparisonWithTheOppositeBranchDirectionIsRejected() = withApk(
        WeChatRuleDexFixture.apk(tabletWidthBranchOpcode = com.android.tools.smali.dexlib2.Opcode.IF_GE),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val missing = assertIs<RuleResolution.Missing>(rule().resolve(session))
            assertEquals(mapOf("tablet" to 0, "login" to 0), missing.candidateCounts)
        }
    }

    @Test
    fun aWidthComparisonThatDoesNotSelectTheReturnedBooleanIsRejected() = withApk(
        WeChatRuleDexFixture.apk(widthComparisonControlsReturnedValue = false),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val missing = assertIs<RuleResolution.Missing>(rule().resolve(session))
            assertEquals(mapOf("tablet" to 0, "login" to 0), missing.candidateCounts)
        }
    }

    @Test
    fun aWidthComparisonUsingAnotherRegisterIsRejectedEvenWhenAllTokensRemain() = withApk(
        WeChatRuleDexFixture.apk(tabletWidthRegister = 4, tabletComparisonRegisterSeed = 601),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val missing = assertIs<RuleResolution.Missing>(rule().resolve(session))
            assertEquals(mapOf("tablet" to 0, "login" to 0), missing.candidateCounts)
        }
    }

    @Test
    fun aDecoy600LiteralCannotSatisfyA601Comparison() = withApk(
        WeChatRuleDexFixture.apk(tabletWidthThreshold = 601, tabletDecoyThreshold = 600),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val missing = assertIs<RuleResolution.Missing>(rule().resolve(session))
            assertEquals(mapOf("tablet" to 0, "login" to 0), missing.candidateCounts)
        }
    }

    @Test
    fun aConditionalBranchIntoAnUnrelatedTrueReturnIsRejected() = withApk(
        WeChatRuleDexFixture.apk(tabletWidthBranchOffset = 4),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val missing = assertIs<RuleResolution.Missing>(rule().resolve(session))
            assertEquals(mapOf("tablet" to 0, "login" to 0), missing.candidateCounts)
        }
    }

    private fun rule(): RuleQuery {
        val objectClass = runCatching {
            Class.forName("io.github.nku100.impad.rules.wechat.WeChatTabletLoginRule")
        }.getOrNull()
        assertNotNull(objectClass, "WeChat tablet/login rule implementation is missing")
        return objectClass.getField("INSTANCE").get(null) as RuleQuery
    }

    private fun syntheticCatalog(
        definitions: List<RuleDefinition>,
        bindings: List<VersionBinding>,
        strategies: Set<String>,
    ) = RuleCatalog(
        appId = "wechat",
        queries = definitions.map { definition ->
            object : RuleQuery {
                override val definition = definition
                override fun resolve(session: RuleSession): RuleResolution = RuleResolution.Missing()
            }
        },
        bindings = bindings,
        strategyIds = strategies,
    )

    private fun assertHistoricalCatalog(catalog: RuleCatalog) {
        assertEquals("wechat", catalog.appId)
        val historical = assertNotNull(
            catalog.definitions.singleOrNull { it.ruleId == HISTORICAL_RULE_ID },
            "Historical WeChat rule definition is missing or duplicated",
        )
        assertEquals(HISTORICAL_STRATEGY_ID, historical.strategyId)
        assertEquals(setOf("tablet", "login"), historical.requiredTargetIds)
        assertTrue(HISTORICAL_STRATEGY_ID in catalog.strategyIds)

        val historicalBinding = assertNotNull(
            catalog.bindings.singleOrNull { it.ruleId == HISTORICAL_RULE_ID },
            "Historical WeChat route is missing or duplicated",
        )
        assertEquals(3040L, historicalBinding.min)
        assertEquals(listOf(HISTORICAL_RULE_ID), VersionRouter.candidates(3040L, catalog.bindings))
    }

    private fun historicalDefinition(
        strategyId: String = HISTORICAL_STRATEGY_ID,
        targets: Set<String> = setOf("tablet", "login"),
    ) = RuleDefinition(HISTORICAL_RULE_ID, strategyId, targets)

    private fun futureDefinition() =
        RuleDefinition(FUTURE_RULE_ID, FUTURE_STRATEGY_ID, setOf("tablet", "login"))

    private fun forwardBindings() = listOf(
        VersionBinding(HISTORICAL_RULE_ID, 3040L, 3040L),
        VersionBinding(FUTURE_RULE_ID, FUTURE_VERSION, null),
    )

    private companion object {
        const val HISTORICAL_RULE_ID = "wechat_tablet_login_001"
        const val HISTORICAL_STRATEGY_ID = "wechat_tablet_login_behavior_001"
        const val FUTURE_RULE_ID = "synthetic_future_rule"
        const val FUTURE_STRATEGY_ID = "synthetic_future_strategy"
        const val FUTURE_VERSION = 5000L
    }

    private fun withApk(apk: File, block: (File) -> Unit) {
        try {
            loadLockedLinuxNative()
            block(apk)
        } finally {
            apk.delete()
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
}
