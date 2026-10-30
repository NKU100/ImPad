package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.AppCompatibilityPolicies
import io.github.nku100.impad.compat.BuildIdentity
import io.github.nku100.impad.rules.RuleExecutionFingerprint
import io.github.nku100.impad.rules.SemanticAppCatalogs
import java.io.File
import kotlin.test.*

class SemanticPlanCacheKeyTest {
    private val catalog = SemanticAppCatalogs.forApp("qq")
    private val execution = RuleExecutionFingerprint.createReport(catalog.sha256, RuleExecutionFingerprint.loadManifest())
    private val build = InstalledBuild(BuildIdentity("com.tencent.mobileqq", "9.3.1", 14378, "arm64-v8a", "a".repeat(64),
        AppCompatibilityPolicies.require("qq").signerSha256), listOf(File("base.apk"), File("split.apk")), "path-based-old-key", File("data"), listOf("a".repeat(64), "b".repeat(64)))

    @Test
    fun contentCatalogImplementationAndIdentityChangesCannotShareAKey() {
        fun key(build: InstalledBuild = this.build, input: String = "c".repeat(64), execution: String = this.execution.cacheIdentitySha256) =
            SemanticPlanCacheKey.create(build, catalog, input, execution)
        val original = key()
        assertEquals(original, key(build.copy(apkFiles = listOf(File("/another/base.apk"), File("/another/split.apk")), dataDirectory = File("elsewhere"), installFingerprint = "different-paths")))
        assertNotEquals(original, key(build.copy(apkFileSha256 = listOf("a".repeat(64), "d".repeat(64)))))
        assertNotEquals(original, key(build.copy(identity = build.identity.copy(versionCode = 14379))))
        assertNotEquals(original, key(input = "e".repeat(64)))
        assertNotEquals(original, key(execution = "f".repeat(64)))
        assertEquals(64, original.length)
    }

    @Test
    fun rejectsIncompleteOrUntrustedIdentityBeforeUsingCache() {
        assertFailsWith<IllegalArgumentException> { SemanticPlanCacheKey.create(build.copy(apkFileSha256 = emptyList()), catalog, "c".repeat(64), execution.cacheIdentitySha256) }
        assertFailsWith<IllegalArgumentException> { SemanticPlanCacheKey.create(build.copy(identity = build.identity.copy(signerSha256 = "f".repeat(64))), catalog, "c".repeat(64), execution.cacheIdentitySha256) }
        assertFailsWith<IllegalArgumentException> { SemanticPlanCacheKey.create(build, catalog, "mtime", execution.cacheIdentitySha256) }
    }
}
