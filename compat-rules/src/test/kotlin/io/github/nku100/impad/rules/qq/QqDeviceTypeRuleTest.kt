package io.github.nku100.impad.rules.qq

import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.rules.DexKitRuntimeLoader
import io.github.nku100.impad.rules.RuleSession
import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

class QqDeviceTypeRuleTest {
    companion object {
        @Synchronized
        private fun loadLockedLinuxNative() {
            if (System.getProperty("impad.dexkit.native.loaded") == "true") return
            check(System.getProperty("os.name").contains("linux", ignoreCase = true)) {
                "Real DEX fixture tests require the task-locked Linux DexKit native"
            }
            val runtimeDirectory = File(checkNotNull(System.getenv("DEXKIT_RUNTIME_DIR")) {
                "Gradle must pass its prepared DexKit output directory to the real DEX tests"
            }).canonicalFile
            Class.forName("org.luckypray.dexkit.DexKitBridge")
            DexKitRuntimeLoader.load(runtimeDirectory)
            System.setProperty("impad.dexkit.native.loaded", "true")
        }
    }

    @Test
    fun resolvesTheStructuralDeviceTypeFeatureFromRealDexDefinitions() = withApk(RuleDexFixture.apk()) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val result = QqDeviceTypeRule.resolve(session)
            val unique = assertIs<RuleResolution.Unique>(result)
            assertEquals(mapOf("device_type" to 1), unique.candidateCounts)
            assertEquals(
                "Lfixture/TabletProbe;->deviceType(Landroid/content/Context;)Lfixture/DeviceType;",
                unique.plan.targets.getValue("device_type").descriptor,
            )
            assertEquals("fixture.DeviceType", unique.plan.targets.getValue("device_type").shape.returnType)
            assertTrue(unique.plan.targets.getValue("device_type").shape.isStatic)
        }
    }

    @Test
    fun missingFeatureAnchorProducesZeroActualDefinitions() = withApk(RuleDexFixture.apk(includeAnchor = false)) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val result = assertIs<RuleResolution.Missing>(QqDeviceTypeRule.resolve(session))
            assertEquals(mapOf("device_type" to 0), result.candidateCounts)
        }
    }

    @Test
    fun twoMatchingDefinitionsRemainAmbiguousWithoutDescriptorDeduplication() = withApk(
        RuleDexFixture.apk(definitions = listOf("Lfixture/TabletProbe;", "Lfixture/TabletProbe2;")),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val result = assertIs<RuleResolution.Ambiguous>(QqDeviceTypeRule.resolve(session))
            assertEquals(mapOf("device_type" to 2), result.candidateCounts)
        }
    }

    @Test
    fun identicalMethodDescriptorsDefinedInTwoDexFilesRemainAmbiguous() = withApk(
        RuleDexFixture.apk(duplicateDefinitionAcrossDex = true),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val result = assertIs<RuleResolution.Ambiguous>(QqDeviceTypeRule.resolve(session))
            assertEquals(mapOf("device_type" to 2), result.candidateCounts)
        }
    }

    @Test
    fun aMethodReferenceDoesNotCountAsTheReferencedDefinition() = withApk(
        RuleDexFixture.apk(definitions = emptyList(), includeReferenceOnlyCaller = true),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val result = assertIs<RuleResolution.Missing>(QqDeviceTypeRule.resolve(session))
            assertEquals(mapOf("device_type" to 0), result.candidateCounts)
        }
    }

    @Test
    fun missingEnumClassRejectsTheMethodEvenWhenTheAnchorExists() = withApk(
        RuleDexFixture.apk(includeEnum = false),
    ) { apk ->
        RuleSession.open(listOf(apk)).use { session ->
            val result = assertIs<RuleResolution.Missing>(QqDeviceTypeRule.resolve(session))
            assertEquals(mapOf("device_type" to 0), result.candidateCounts)
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
}
