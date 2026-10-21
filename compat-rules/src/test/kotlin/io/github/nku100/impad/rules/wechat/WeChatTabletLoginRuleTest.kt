package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.rules.RuleCatalog
import io.github.nku100.impad.rules.RuleQuery
import io.github.nku100.impad.rules.RuleSession
import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertNotNull

class WeChatTabletLoginRuleTest {
    @Test
    fun catalogRoutesWechatToTheImmutable3040TabletLoginRule() {
        val catalog = runCatching { RuleCatalog.forApp("wechat") }.getOrNull()
        assertNotNull(catalog, "WeChat rule catalog route is missing")
        assertEquals("wechat", catalog.appId)
        assertEquals(setOf("wechat_tablet_login_001"), catalog.definitions.map { it.ruleId }.toSet())
        assertEquals(setOf("wechat_tablet_login_behavior_001"), catalog.strategyIds)
        assertEquals(setOf("tablet", "login"), catalog.definitions.single().requiredTargetIds)
        assertEquals(3040L, catalog.bindings.single().min)
        assertEquals(null, catalog.bindings.single().max)
    }

    @Test
    fun resolvesBothTargetsOnlyWhenTheirDexSemanticsAndCallRelationshipMatch() = withApk(WeChatRuleDexFixture.apk()) { apk ->
        val catalog = RuleCatalog.forApp("wechat")
        RuleSession.open(listOf(apk)).use { session ->
            val selected = assertIs<RuleResolution.Unique>(rule().resolve(session))
            assertEquals(mapOf("tablet" to 1, "login" to 1), selected.candidateCounts)
            assertEquals(catalog.definitions.single().ref, selected.plan.rule)
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
