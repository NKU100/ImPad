package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.AppCompatibilityPolicies
import io.github.nku100.impad.compat.BuildIdentity
import io.github.nku100.impad.compat.rules.MethodShape
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.ResolvedTarget
import io.github.nku100.impad.compat.rules.RuleDefinition
import io.github.nku100.impad.compat.rules.RuleRef
import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.compat.rules.VersionBinding
import io.github.nku100.impad.rules.RuleCatalog
import io.github.nku100.impad.rules.RuleExecutionFingerprint
import io.github.nku100.impad.rules.RuleMethod
import io.github.nku100.impad.rules.RuleClass
import io.github.nku100.impad.rules.MethodQuery
import io.github.nku100.impad.rules.RuleQuery
import io.github.nku100.impad.rules.RuleSession
import java.nio.file.Files
import java.util.Comparator
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertTrue
import org.junit.jupiter.api.Test

class RuleRuntimeResolverTest {
    @Test
    fun cachesAPlanByEveryBaseAndSplitContentHash() = withFiles { directory, base, split ->
        val cache = RuleResolutionCache(directory.resolve("cache").toFile())
        val fingerprint = RuleExecutionFingerprint.createReport(catalog().sha256, RuleExecutionFingerprint.loadManifest())
        var sessionOpens = 0
        val resolver = RuleRuntimeResolver(catalog(), cache, fingerprint) {
            sessionOpens++
            EmptySession
        }
        val original = build(base, split, "a".repeat(64))

        val first = resolver.resolve(original)
        val cached = resolver.resolve(original)
        val changedSplit = resolver.resolve(build(base, split, "b".repeat(64)))

        assertIs<io.github.nku100.impad.compat.rules.SelectionOutcome.Selected>(first.outcome)
        assertTrue(cached.cacheHit)
        assertFalse(first.cacheHit)
        assertFalse(changedSplit.cacheHit)
        assertEquals(2, sessionOpens)
    }

    @Test
    fun cacheWriteFailureDoesNotChangeRuleSelection() = withFiles { directory, base, split ->
        val blockedPath = directory.resolve("not-a-directory")
        Files.writeString(blockedPath, "file")
        val cache = RuleResolutionCache(blockedPath.resolve("cache").toFile())
        val fingerprint = RuleExecutionFingerprint.createReport(catalog().sha256, RuleExecutionFingerprint.loadManifest())
        val resolver = RuleRuntimeResolver(catalog(), cache, fingerprint) { EmptySession }

        val result = resolver.resolve(build(base, split, "a".repeat(64)))

        assertIs<io.github.nku100.impad.compat.rules.SelectionOutcome.Selected>(result.outcome)
        assertFalse(result.cacheHit)
    }

    @Test
    fun rejectsAnUntrustedSignerBeforeOpeningDexKitSession() = withFiles { directory, base, split ->
        val cache = RuleResolutionCache(directory.resolve("cache").toFile())
        val fingerprint = RuleExecutionFingerprint.createReport(catalog().sha256, RuleExecutionFingerprint.loadManifest())
        var sessionOpens = 0
        val resolver = RuleRuntimeResolver(catalog(), cache, fingerprint) {
            sessionOpens++
            EmptySession
        }

        kotlin.test.assertFailsWith<IllegalArgumentException> {
            resolver.resolve(build(base, split, "a".repeat(64)).copy(
                identity = build(base, split, "a".repeat(64)).identity.copy(signerSha256 = "f".repeat(64)),
            ))
        }
        assertEquals(0, sessionOpens)
    }

    private fun catalog(): RuleCatalog {
        val definition = RuleDefinition("qq_device_type_001", "qq_tablet_001", setOf("device_type"))
        val query = object : RuleQuery {
            override val definition = definition
            override fun resolve(session: RuleSession) = RuleResolution.Unique(
                ResolvedPlan(
                    RuleRef(definition.ruleId, definition.strategyId),
                    mapOf("device_type" to ResolvedTarget(
                        "Lfixture/Business;->device()Z",
                        MethodShape(emptyList(), "boolean", true),
                    )),
                ),
                mapOf("device_type" to 1),
            )
        }
        return RuleCatalog(
            appId = "qq",
            queries = listOf(query),
            bindings = listOf(VersionBinding(definition.ruleId, 14378, null)),
            strategyIds = setOf(definition.strategyId),
            policy = AppCompatibilityPolicies.require("qq"),
        )
    }

    private fun build(base: java.io.File, split: java.io.File, splitSha: String) = InstalledBuild(
        identity = BuildIdentity(
            packageName = "com.tencent.mobileqq",
            versionName = "9.3.1",
            versionCode = 14378,
            abi = "arm64-v8a",
            apkSha256 = "a".repeat(64),
            signerSha256 = AppCompatibilityPolicies.require("qq").signerSha256,
        ),
        apkFiles = listOf(base, split),
        installFingerprint = "0".repeat(64),
        dataDirectory = requireNotNull(base.parentFile),
        apkFileSha256 = listOf("a".repeat(64), splitSha),
    )

    private fun withFiles(block: (java.nio.file.Path, java.io.File, java.io.File) -> Unit) {
        val directory = Files.createTempDirectory("impad rule runtime")
        try {
            val base = directory.resolve("base.apk")
            val split = directory.resolve("split.apk")
            Files.write(base, byteArrayOf(1))
            Files.write(split, byteArrayOf(2))
            block(directory, base.toFile(), split.toFile())
        } finally {
            Files.walk(directory).use { paths ->
                paths.sorted(Comparator.reverseOrder<java.nio.file.Path>()).forEach(Files::deleteIfExists)
            }
        }
    }

    private object EmptySession : RuleSession {
        override fun findMethods(query: MethodQuery): List<RuleMethod> = emptyList()
        override fun findClasses(typeName: String): List<RuleClass> = emptyList()
        override fun close() = Unit
    }
}
