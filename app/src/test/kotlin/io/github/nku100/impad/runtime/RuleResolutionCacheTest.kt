package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.rules.MethodShape
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.ResolvedTarget
import io.github.nku100.impad.compat.rules.RuleRef
import java.nio.file.Files
import java.util.Comparator
import kotlin.test.assertEquals
import kotlin.test.assertNull
import org.junit.jupiter.api.Test

class RuleResolutionCacheTest {
    @Test
    fun reusesOnlyPlansWithAnExactRuntimeIdentity() = withCache { cache, _ ->
        val firstKey = key(baseSha = "a".repeat(64))
        val plan = plan()
        cache.write(firstKey, plan)

        assertEquals(plan, cache.read(firstKey))
        assertNull(cache.read(firstKey.copy(apkInputSha256 = listOf("b".repeat(64)))))
        assertNull(cache.read(firstKey.copy(strategyImplementationSha256 = "c".repeat(64))))
        assertNull(cache.read(firstKey.copy(routeRuleIds = listOf("different_rule"))))
        assertNull(cache.read(firstKey.copy(dexkitLockSha256 = "9".repeat(64))))
    }

    @Test
    fun ignoresCorruptAndLegacyCacheFiles() = withCache { cache, directory ->
        directory.resolve("resolution-qq.json")
            .writeText("{\"status\":\"COMPATIBLE\",\"resolvedDescriptors\":{}}")

        assertNull(cache.read(key(baseSha = "a".repeat(64))))
    }

    @Test
    fun doesNotReuseAnExpiredPlan() {
        val directory = Files.createTempDirectory("impad expired rule cache")
        try {
            var now = 1_000L
            val cache = RuleResolutionCache(directory.toFile(), nowMillis = { now }, maxAgeMillis = 10)
            val key = key(baseSha = "a".repeat(64))
            cache.write(key, plan())
            now += 11

            assertNull(cache.read(key))
        } finally {
            Files.walk(directory).use { paths ->
                paths.sorted(Comparator.reverseOrder<java.nio.file.Path>()).forEach(Files::deleteIfExists)
            }
        }
    }

    private fun withCache(block: (RuleResolutionCache, java.io.File) -> Unit) {
        val directory = Files.createTempDirectory("impad rule cache")
        try {
            block(RuleResolutionCache(directory.toFile()), directory.toFile())
        } finally {
            Files.walk(directory).use { paths ->
                paths.sorted(Comparator.reverseOrder<java.nio.file.Path>()).forEach(Files::deleteIfExists)
            }
        }
    }

    private fun key(baseSha: String) = RuleResolutionCacheKey(
        appId = "qq",
        packageName = "com.tencent.mobileqq",
        abi = "arm64-v8a",
        signerSha256 = "d".repeat(64),
        versionCode = 14378,
        apkInputSha256 = listOf(baseSha, "e".repeat(64)),
        routeRuleIds = listOf("qq_device_type_001"),
        routeStrategyIds = listOf("qq_tablet_001"),
        ruleCatalogSha256 = "f".repeat(64),
        ruleImplementationSha256 = "1".repeat(64),
        resolverImplementationSha256 = "2".repeat(64),
        strategyImplementationSha256 = "3".repeat(64),
        dexkitApiSha256 = "4".repeat(64),
        dexkitNativeSha256 = "5".repeat(64),
        dexkitSourceCommit = "6".repeat(40),
        dexkitLockSha256 = "7".repeat(64),
        executionIdentitySha256 = "8".repeat(64),
    )

    private fun plan() = ResolvedPlan(
        RuleRef("qq_device_type_001", "qq_tablet_001"),
        mapOf("device_type" to ResolvedTarget(
            "Lfixture/Business;->device()Z",
            MethodShape(emptyList(), "boolean", true),
        )),
    )
}
