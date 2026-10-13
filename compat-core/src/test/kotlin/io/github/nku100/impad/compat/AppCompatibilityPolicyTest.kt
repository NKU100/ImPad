package io.github.nku100.impad.compat

import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNotNull
import kotlin.test.assertTrue
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json

class AppCompatibilityPolicyTest {
    @Test
    fun validatesASecondPolicyWithoutWeChatPackageAbiOrHookAssumptions() {
        val policy = AppCompatibilityPolicy(
            appId = "testapp",
            packageName = "org.example.testapp",
            supportedAbis = setOf("x86_64"),
            signerSha256 = SIGNER,
            requiredHookIds = setOf("route", "session"),
            officialApkPrefix = "https://downloads.example.org/testapp/",
            targetsPath = "compatibility/testapp/targets.json",
        )
        policy.validateTargets(listOf(target()))
        policy.validateIdentity(target().identity)
        policy.validateSourceUrl("https://downloads.example.org/testapp/candidate.apk")

        assertFailsWith<IllegalArgumentException> {
            policy.validateTargets(listOf(target().copy(hooks = target().hooks.take(1))))
        }
    }

    @Test
    fun productionRegistryRejectsUnknownApplications() {
        val error = assertFailsWith<IllegalArgumentException> { AppCompatibilityPolicies.require("unregistered") }
        assertTrue(error.message!!.contains("Unknown or unregistered appId"))
    }

    @Test
    fun registeredWeChatPolicyValidatesTheProductionManifestAndRetainsTheOriginalProfile() {
        val targets = productionTargets()
        AppCompatibilityPolicies.require("wechat").validateTargets(targets)
        assertNotNull(targets.singleOrNull {
            it.identity.packageName == "com.tencent.mm" &&
                it.identity.abi == "arm64-v8a" &&
                it.identity.versionCode == 3200L &&
                it.identity.apkSha256 == ORIGINAL_8_0_79_SHA256
        })
    }

    @Test
    fun productionManifestRetainsExistingProfilesWhenAddingSameCodeRepack() {
        val targets = productionTargets()
        val original = targets.single {
            it.identity.packageName == "com.tencent.mm" &&
                it.identity.abi == "arm64-v8a" &&
                it.identity.versionCode == 3200L &&
                it.identity.apkSha256 == ORIGINAL_8_0_79_SHA256
        }
        val originalCodeProfiles = targets.filter {
            it.identity.packageName == original.identity.packageName &&
                it.identity.abi == original.identity.abi &&
                it.identity.versionCode == original.identity.versionCode
        }
        val originalDigests = originalCodeProfiles.map { requireNotNull(it.identity.apkSha256).lowercase() }.toSet()
        val existingVariantDigest = nextDigest(originalDigests)
        val existingVariant = original.copy(identity = original.identity.copy(apkSha256 = existingVariantDigest))
        val profilesWithExistingVariant = targets + existingVariant
        val beforeProfiles = profilesWithExistingVariant.filter {
            it.identity.packageName == original.identity.packageName &&
                it.identity.abi == original.identity.abi &&
                it.identity.versionCode == original.identity.versionCode
        }
        val beforeDigests = beforeProfiles.map { requireNotNull(it.identity.apkSha256).lowercase() }.toSet()
        val repackDigest = nextDigest(beforeDigests)
        val repack = original.copy(identity = original.identity.copy(apkSha256 = repackDigest))
        val profiles = profilesWithExistingVariant + repack

        AppCompatibilityPolicies.require("wechat").validateTargets(profiles)

        val sameVersionProfiles = profiles.filter {
            it.identity.packageName == original.identity.packageName &&
                it.identity.abi == original.identity.abi &&
                it.identity.versionCode == original.identity.versionCode
        }
        val resultingDigests = sameVersionProfiles.map { requireNotNull(it.identity.apkSha256).lowercase() }.toSet()
        assertEquals(beforeProfiles.size + 1, sameVersionProfiles.size)
        assertEquals(beforeDigests + repackDigest, resultingDigests)
        assertNotNull(sameVersionProfiles.singleOrNull {
            it.identity.packageName == original.identity.packageName &&
                it.identity.abi == original.identity.abi &&
                it.identity.versionCode == original.identity.versionCode &&
                it.identity.apkSha256.equals(ORIGINAL_8_0_79_SHA256, ignoreCase = true)
        })
        assertNotNull(sameVersionProfiles.singleOrNull {
            it.identity.packageName == original.identity.packageName &&
                it.identity.abi == original.identity.abi &&
                it.identity.versionCode == original.identity.versionCode &&
                it.identity.apkSha256.equals(repackDigest, ignoreCase = true)
        })
        assertNotNull(sameVersionProfiles.singleOrNull {
            it.identity.packageName == original.identity.packageName &&
                it.identity.abi == original.identity.abi &&
                it.identity.versionCode == original.identity.versionCode &&
                it.identity.apkSha256.equals(existingVariantDigest, ignoreCase = true)
        })
    }

    @Test
    fun permitsDistinctDigestsAtTheSameVersionAndRejectsCaseInsensitiveDuplicates() {
        val policy = AppCompatibilityPolicies.require("wechat")
        val first = productionTarget("a".repeat(64))
        val repack = productionTarget("b".repeat(64))
        policy.validateTargets(listOf(first, repack))

        val duplicate = productionTarget("A".repeat(64))
        assertFailsWith<IllegalArgumentException> {
            policy.validateTargets(listOf(first, duplicate))
        }
    }

    private fun productionTarget(digest: String) = CompatibilityTarget(
        identity = BuildIdentity("com.tencent.mm", "8.0.79", 3200, "arm64-v8a", digest,
            "0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c"),
        featureRulesVersion = 1,
        hooks = listOf(
            HookRule("tablet", "tablet", emptyList(), "Z", "Lfixture/Tablet;->check()Z"),
            HookRule("login", "login", emptyList(), "V", "Lfixture/Login;->open()V"),
        ),
        sourceUrl = "https://dldir1v6.qq.com/weixin/android/repack.apk",
    )

    private fun productionTargets(): List<CompatibilityTarget> {
        val manifest = listOf(File("compatibility/wechat/targets.json"), File("../compatibility/wechat/targets.json"))
            .firstOrNull(File::isFile)
        assertNotNull(manifest, "Production WeChat targets are available to core tests")
        return Json.decodeFromString(manifest.readText())
    }

    private fun nextDigest(usedDigests: Set<String>): String = generateSequence(0L) { it + 1 }
        .map { it.toString(16).padStart(64, '0') }
        .first { it !in usedDigests }

    private fun target() = CompatibilityTarget(
        identity = BuildIdentity(
            packageName = "org.example.testapp",
            versionName = "2.4",
            versionCode = 24,
            abi = "x86_64",
            apkSha256 = APK,
            signerSha256 = SIGNER,
        ),
        featureRulesVersion = 1,
        hooks = listOf(
            HookRule("route", "route", emptyList(), "V", "Lfixture/Route;->run()V"),
            HookRule("session", "session", emptyList(), "Z", "Lfixture/Session;->valid()Z"),
        ),
        sourceUrl = "https://downloads.example.org/testapp/base.apk",
    )

    private companion object {
        val APK = "a".repeat(64)
        val SIGNER = "b".repeat(64)
        const val ORIGINAL_8_0_79_SHA256 = "5feb100337981467fd257c3ad66bb171f54a69d2579b2ecc70d5a628db8e7282"
    }
}
