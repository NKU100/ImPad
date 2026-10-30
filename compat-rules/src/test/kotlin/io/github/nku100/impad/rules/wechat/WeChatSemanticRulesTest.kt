package io.github.nku100.impad.rules.wechat

import com.android.tools.smali.dexlib2.Opcodes
import com.android.tools.smali.dexlib2.dexbacked.DexBackedDexFile
import com.android.tools.smali.dexlib2.immutable.ImmutableDexFile
import com.android.tools.smali.dexlib2.writer.pool.DexPool
import io.github.nku100.impad.compat.BuildIdentity
import io.github.nku100.impad.compat.code.CodeDelegation
import io.github.nku100.impad.compat.rules.CompletePlanSelection
import io.github.nku100.impad.rules.*
import java.io.File
import java.nio.file.Files
import java.util.zip.ZipEntry
import java.util.zip.ZipFile
import java.util.zip.ZipOutputStream
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

class WeChatSemanticRulesTest {
    @Test
    fun directAndCachedVariantsUseSameContractAndHookTheWrapper() {
        for (options in listOf(
            WeChatForwardRuleDexFixture.Options(),
            WeChatForwardRuleDexFixture.Options(reloadCachedResult = true, cachedWrapper = true),
            WeChatForwardRuleDexFixture.Options(reloadCachedResult = true, cachedWrapper = true, wrapperForceTablet = true),
        )) withAggregate(options) { snapshot, session ->
            val selected = assertIs<CompletePlanSelection.Selected>(run(snapshot, session))
            assertEquals("wechat_tablet_login_callbacks", selected.match.plan.strategyId)
            assertEquals("Lfixture/TabletDecision;->isTablet()Z", selected.match.plan.hooks.first().descriptor)
            assertTrue(selected.match.plan.hooks.last().descriptor.startsWith("Lfixture/LoginSetup;->update("))
        }
    }

    @Test
    fun twoDecisionsButOnlyOneCompleteLoginRelationSelectsOnePlan() = withAggregate(
        WeChatForwardRuleDexFixture.Options(tabletOwners = listOf("Lfixture/First;", "Lfixture/Second;")),
    ) { snapshot, session ->
        val selected = assertIs<CompletePlanSelection.Selected>(run(snapshot, session))
        assertEquals("Lfixture/First;->isTablet()Z", selected.match.plan.hooks.first().descriptor)
    }

    @Test
    fun paddedBooleanPathReturnsIncompleteInsteadOfNoMatch() = withAggregate(
        WeChatForwardRuleDexFixture.Options(returnPathPadding = 80),
    ) { snapshot, session ->
        assertIs<CompletePlanSelection.Incomplete>(run(snapshot, session))
    }

    @Test
    fun twoCompleteLoginRelationsConflict() = withAggregate(
        WeChatForwardRuleDexFixture.Options(loginOwners = listOf("Lfixture/FirstLogin;", "Lfixture/SecondLogin;")),
    ) { snapshot, session ->
        assertEquals(2, assertIs<CompletePlanSelection.Conflict>(run(snapshot, session)).plans.size)
    }

    @Test
    fun brokenRelationshipOrMissingWidthProofNeverInstallsPartialPlan() {
        for (options in listOf(
            WeChatForwardRuleDexFixture.Options(wrapperForwardsResult = false),
            WeChatForwardRuleDexFixture.Options(loginCallsTabletHelper = false),
            WeChatForwardRuleDexFixture.Options(widthControlsResult = false),
            WeChatForwardRuleDexFixture.Options(loginBooleanControlsVisibility = false),
            WeChatForwardRuleDexFixture.Options(cachedWrapper = true, wrapperEarlyReturn = true),
        )) withAggregate(options) { snapshot, session -> assertIs<CompletePlanSelection.NoMatch>(run(snapshot, session)) }
    }

    @Test
    fun inlineLegacyShapeAlsoTargetsTheProvedWrapper() {
        val apk = WeChatRuleDexFixture.apk(proveCallFlow = true)
        try { withInputs(listOf(CodeLoaderInput("ready", null, CodeDelegation.PARENT_FIRST, listOf(apk))), "ready") { snapshot, session ->
            val selected = assertIs<CompletePlanSelection.Selected>(run(snapshot, session))
            assertTrue(selected.match.plan.hooks.first().descriptor.contains("->tabletEnabled()Z"))
        } } finally { apk.delete() }
    }

    @Test
    fun parentLoginCannotLinkToChildDefinitionWithSameDescriptor() {
        val parent = WeChatForwardRuleDexFixture.apk()
        val child = Files.createTempFile("wechat-child-tablet-", ".apk").toFile()
        val dex = Files.createTempFile("wechat-child-tablet-", ".dex")
        try {
            val classes = ZipFile(parent).use { zip -> zip.getInputStream(zip.getEntry("classes.dex")).buffered().use {
                DexBackedDexFile.fromInputStream(Opcodes.getDefault(), it).classes.filter { it.type == "Lfixture/TabletDecision;" }
            } }
            DexPool.writeTo(dex.toString(), ImmutableDexFile(Opcodes.getDefault(), classes))
            ZipOutputStream(child.outputStream()).use { zip ->
                zip.putNextEntry(ZipEntry("classes.dex")); Files.copy(dex, zip); zip.closeEntry()
            }
            withInputs(listOf(CodeLoaderInput("parent", null, CodeDelegation.PARENT_FIRST, listOf(parent)),
                CodeLoaderInput("child", "parent", CodeDelegation.DELEGATE_LAST, listOf(child))), "child") { snapshot, session ->
                val selected = assertIs<CompletePlanSelection.Selected>(run(snapshot, session))
                assertTrue(selected.match.plan.hooks.all { it.source.loaderId == "parent" })
            }
        } finally { parent.delete(); child.delete(); Files.deleteIfExists(dex) }
    }

    private fun withAggregate(options: WeChatForwardRuleDexFixture.Options, block: (CapturedCodeSnapshot, RuleSession) -> Unit) {
        val apk = WeChatForwardRuleDexFixture.apk(options)
        try { withInputs(listOf(CodeLoaderInput("ready", null, CodeDelegation.PARENT_FIRST, listOf(apk))), "ready", block) }
        finally { apk.delete() }
    }

    private fun withInputs(loaders: List<CodeLoaderInput>, ready: String, block: (CapturedCodeSnapshot, RuleSession) -> Unit) {
        StartupCodeSnapshot().capture(ready, loaders).use { snapshot ->
            EffectiveCodeRuleSession.open(snapshot) { file ->
                object : RuleSessionBackend {
                    private val delegate = FixtureDexSession(file)
                    override fun findMethods(query: MethodQuery) = delegate.findMethods(query)
                    override fun findClasses(typeName: String) = delegate.findClasses(typeName)
                    override fun readInstructions(descriptor: String) = delegate.readInstructions(delegate.findMethods(MethodQuery()).single { it.descriptor == descriptor })
                    override fun close() = delegate.close()
                }
            }.use { block(snapshot, it) }
        }
    }

    private fun run(snapshot: CapturedCodeSnapshot, session: RuleSession): CompletePlanSelection {
        val catalog = WeChatSemanticRules.catalog
        val identity = BuildIdentity("com.tencent.mm", "test", 3200, "arm64-v8a", "a".repeat(64),
            "0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c")
        return SemanticRuleResolver.resolveDetailed(identity, catalog, RecognitionContext(session, snapshot, catalog)).outcome
    }
}
