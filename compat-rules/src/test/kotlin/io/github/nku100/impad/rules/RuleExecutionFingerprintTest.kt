package io.github.nku100.impad.rules

import java.io.File
import java.security.MessageDigest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotEquals
import kotlin.test.assertTrue
import kotlin.test.assertFailsWith

class RuleExecutionFingerprintTest {
    @Test
    fun changingQuerySourceChangesImplementationIdentityWithoutChangingCatalogDeclaration() {
        val catalogSha = RuleCatalog.forApp("qq").sha256
        val original = fingerprint(catalogSha, ruleSourceSha = "a".repeat(64))
        val changedQuery = fingerprint(catalogSha, ruleSourceSha = "b".repeat(64))

        assertEquals(catalogSha, original.ruleCatalogSha256)
        assertEquals(original.ruleCatalogSha256, changedQuery.ruleCatalogSha256)
        assertNotEquals(original.ruleImplementationSha256, changedQuery.ruleImplementationSha256)
        assertNotEquals(original.cacheIdentitySha256, changedQuery.cacheIdentitySha256)
    }

    @Test
    fun changingDexKitLockChangesDependencyAndCacheIdentity() {
        val catalogSha = RuleCatalog.forApp("qq").sha256
        val original = fingerprint(catalogSha, lockSha = "c".repeat(64))
        val changedLock = fingerprint(catalogSha, lockSha = "d".repeat(64))

        assertEquals(catalogSha, changedLock.ruleCatalogSha256)
        assertNotEquals(original.dexkitLockSha256, changedLock.dexkitLockSha256)
        assertNotEquals(original.cacheIdentitySha256, changedLock.cacheIdentitySha256)
    }

    @Test
    fun changingTheRealQqCallbackSourceChangesStrategyAndCacheIdentity() {
        val catalogSha = RuleCatalog.forApp("qq").sha256
        val original = fingerprint(catalogSha, strategySourceSha = "a".repeat(64))
        val changedCallback = fingerprint(catalogSha, strategySourceSha = "b".repeat(64))

        assertNotEquals(original.strategyImplementationSha256, changedCallback.strategyImplementationSha256)
        assertNotEquals(original.cacheIdentitySha256, changedCallback.cacheIdentitySha256)
    }

    @Test
    fun runtimeFingerprintIsGeneratedFromRuleResolverSourcesAndDexKitLock() {
        val manifest = RuleExecutionFingerprint.loadManifest()
        val report = RuleExecutionFingerprint.createReport(RuleCatalog.forApp("qq").sha256, manifest)

        assertTrue(manifest.ruleSources.any { it.path.endsWith("QqDeviceTypeRule.kt") })
        assertTrue(manifest.ruleSources.any { it.path.endsWith("RuleSession.kt") })
        assertTrue(manifest.resolverSources.any { it.path.endsWith("RuleSelector.kt") })
        assertTrue(manifest.strategySources.any { it.path.endsWith("QqHookInstaller.kt") })
        assertFalse(manifest.moduleSources.any { it.path == "compatibility/resolution-modes.json" })
        assertTrue(manifest.moduleSources.any { it.path.endsWith("RuleExecutionFingerprint.kt") })
        assertTrue(manifest.moduleSources.any {
            it.path == "app/src/release/keepRules/impad-module-entry.keep"
        })
        assertEquals(manifest.sourceTreeSha256, digestSources(manifest.moduleSources))
        assertEquals(64, report.ruleImplementationSha256.length)
        assertEquals(64, report.sharedResolverImplementationSha256.length)
        assertEquals(64, report.strategyImplementationSha256.length)
        assertEquals(64, report.sourceTreeSha256.length)
        assertEquals(64, report.routeConfigurationSha256.length)
        assertEquals(64, report.dependencyLockSha256.length)
        assertEquals(RuleExecutionFingerprint.fingerprintFileSha256(), report.fingerprintFileSha256)
        assertEquals(64, report.dexkitLockSha256.length)
        assertEquals("c9cd12a14b75409bebd2f73e4dfc5ff575df3eb8", report.dexkitSourceCommit)
        assertEquals("34a8c16d548aa7d8562128f4d2293cec4e0f9896bb1f46224dcd532e878900f5", report.dexkitApiSha256)
        assertEquals(manifest.dexkitLinuxX8664NativeSha256, report.dexkitLinuxX8664NativeSha256)
    }

    @Test
    fun generatedFingerprintIncludesEveryQqStrategySourceFile() {
        val projectRoot = File("..").canonicalFile
        val qqSourceRoot = File(
            projectRoot,
            "app/src/main/kotlin/io/github/nku100/impad/apps/qq",
        )
        val expectedPaths = qqSourceRoot.walkTopDown()
            .filter { it.isFile && it.extension == "kt" && !it.name.startsWith("._") }
            .map { it.relativeTo(projectRoot).invariantSeparatorsPath }
            .toSet()
        val manifestPaths = RuleExecutionFingerprint.loadManifest().strategySources
            .map(SourceFileSha256::path)
            .toSet()

        assertTrue(manifestPaths.containsAll(expectedPaths))
        assertTrue(manifestPaths.containsAll(setOf(
            "app/src/main/kotlin/io/github/nku100/impad/runtime/HookCall.kt",
            "app/src/main/kotlin/io/github/nku100/impad/runtime/HookInstaller.kt",
        )))

        val probePath = System.getenv("IMPAD_STRATEGY_FINGERPRINT_PROBE")
        if (probePath != null) {
            assertTrue(probePath in expectedPaths)
            assertTrue(probePath in manifestPaths)
            val manifest = RuleExecutionFingerprint.loadManifest()
            val catalogSha = RuleCatalog.forApp("qq").sha256
            val generated = RuleExecutionFingerprint.createReport(catalogSha, manifest)
            val withoutProbe = RuleExecutionFingerprint.createReport(
                catalogSha,
                manifest.copy(strategySources = manifest.strategySources.filterNot { it.path == probePath }),
            )
            assertNotEquals(generated.strategyImplementationSha256, withoutProbe.strategyImplementationSha256)
            assertNotEquals(generated.cacheIdentitySha256, withoutProbe.cacheIdentitySha256)
        }
    }

    @Test
    fun generatedFingerprintIncludesEveryWeChatStrategySourceFile() {
        val projectRoot = File("..").canonicalFile
        val wechatSourceRoot = File(
            projectRoot,
            "app/src/main/kotlin/io/github/nku100/impad/apps/wechat",
        )
        val expectedPaths = wechatSourceRoot.walkTopDown()
            .filter { it.isFile && it.extension == "kt" && !it.name.startsWith("._") }
            .map { it.relativeTo(projectRoot).invariantSeparatorsPath }
            .toSet()
        val manifestPaths = RuleExecutionFingerprint.loadManifest().strategySources
            .map(SourceFileSha256::path)
            .toSet()

        assertTrue(expectedPaths.isNotEmpty())
        assertTrue(manifestPaths.containsAll(expectedPaths))
    }

    @Test
    fun loadedRuntimeMustMatchTheBuildGeneratedDexKitLock() {
        val manifest = RuleExecutionFingerprint.loadManifest()
        val runtime = DexKitRuntimeIdentity(
            "linux-x86_64",
            manifest.dexkitSourceCommit,
            manifest.dexkitApiSha256,
            manifest.dexkitLinuxX8664NativeSha256,
        )
        val report = RuleExecutionFingerprint.createReport(RuleCatalog.forApp("qq").sha256, manifest, runtime)

        assertEquals("linux-x86_64", report.dexkitPlatform)
        assertEquals(runtime.nativeSha256, report.dexkitNativeSha256)
        assertEquals(64, report.cacheIdentitySha256.length)
        assertFailsWith<IllegalArgumentException> {
            RuleExecutionFingerprint.createReport(
                RuleCatalog.forApp("qq").sha256,
                manifest,
                runtime.copy(nativeSha256 = "0".repeat(64)),
            )
        }
    }

    private fun fingerprint(
        catalogSha: String,
        ruleSourceSha: String = "a".repeat(64),
        lockSha: String = "e".repeat(64),
        strategySourceSha: String = "9".repeat(64),
    ) = RuleExecutionFingerprint.createReport(
        catalogSha,
        RuleExecutionManifest(
            schemaVersion = 1,
            ruleSources = listOf(SourceFileSha256("qq/QqDeviceTypeRule.kt", ruleSourceSha)),
            resolverSources = listOf(SourceFileSha256("compat/RuleSelector.kt", "f".repeat(64))),
            strategySources = listOf(SourceFileSha256("QqHookInstaller.kt", strategySourceSha)),
            moduleSources = listOf(SourceFileSha256("module.gradle.kts", "8".repeat(64))),
            sourceTreeSha256 = digestSources(listOf(SourceFileSha256("module.gradle.kts", "8".repeat(64)))),
            routeConfigurationSha256 = "7".repeat(64),
            dependencyLockSha256 = "6".repeat(64),
            dexkitLockSha256 = lockSha,
            dexkitSourceCommit = "c9cd12a14b75409bebd2f73e4dfc5ff575df3eb8",
            dexkitApiSha256 = "1".repeat(64),
            dexkitNativeSha256 = "2".repeat(64),
            dexkitPlatform = "linux-x86_64",
        ),
    )

    private fun digestSources(sources: List<SourceFileSha256>): String = MessageDigest.getInstance("SHA-256")
        .digest(sources.sortedBy(SourceFileSha256::path).joinToString("") { "${it.path}\n${it.sha256}\n" }.toByteArray())
        .joinToString("") { "%02x".format(it) }
}
