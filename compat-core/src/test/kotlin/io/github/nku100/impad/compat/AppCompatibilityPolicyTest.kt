package io.github.nku100.impad.compat

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

class AppCompatibilityPolicyTest {
    @Test
    fun policyHasNoProfileOrResolutionModeSurface() {
        val forbidden = setOf("resolutionMode", "targetsPath", "validateTargets", "validateLocalTargets")
        assertTrue(AppCompatibilityPolicy::class.java.methods.none { it.name in forbidden } &&
            AppCompatibilityPolicy::class.java.declaredFields.none { it.name in forbidden })
    }

    @Test
    fun allRegisteredAppsHaveAnExplicitRulePolicy() {
        assertEquals(setOf("wechat", "qq", "wecom"),
            setOf("wechat", "qq", "wecom").map { AppCompatibilityPolicies.require(it).appId }.toSet())
        val error = assertFailsWith<IllegalArgumentException> { AppCompatibilityPolicies.require("unregistered") }
        assertTrue(error.message.orEmpty().contains("Unknown or unregistered appId"))
    }

    @Test
    fun policyValidatesInstalledIdentityAndOfficialApkUrl() {
        val policy = AppCompatibilityPolicies.require("wechat")
        val identity = BuildIdentity("com.tencent.mm", "8.0.69", 3040, "arm64-v8a", "a".repeat(64),
            "0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c")
        policy.validateIdentity(identity)
        policy.validateSourceUrl("https://dldir1v6.qq.com/weixin/android/wechat.apk")
        assertFailsWith<IllegalArgumentException> {
            policy.validateSourceUrl("https://example.org/wechat.apk")
        }
    }
}
