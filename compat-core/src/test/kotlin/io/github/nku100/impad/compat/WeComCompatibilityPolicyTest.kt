package io.github.nku100.impad.compat

import io.github.nku100.impad.compat.apps.wecom.WeComCompatibilityPolicy
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class WeComCompatibilityPolicyTest {
    @Test
    fun validatesTheExactLocalOnlyWeComBuildAndDescriptorRules() {
        val policy = AppCompatibilityPolicies.require("wecom")
        val target = target()

        assertEquals("com.tencent.wework", policy.packageName)
        assertEquals(setOf("device_type", "app_pad", "service_pad"), policy.requiredHookIds)
        policy.validateLocalTargets(listOf(target))
        policy.validateIdentity(target.identity)
        policy.validateSourceUrl(OFFICIAL_APK_URL)
        assertFailsWith<IllegalArgumentException> { policy.validateTargets(listOf(target)) }
    }

    @Test
    fun validatesTheSelected508TargetWithoutDuplicatingItsPinnedIdentity() {
        val policy = AppCompatibilityPolicies.require("wecom")
        val target = target().copy(
            identity = target().identity.copy(
                versionName = "5.0.8",
                versionCode = 70675,
                apkSha256 = APK_508_SHA256,
            ),
        )

        policy.validateLocalTargets(listOf(target))
        policy.validateIdentity(target.identity)
    }

    @Test
    fun rejectsDescriptorRulesThatCanForwardInferOrCarryStringAnchors() {
        val policy = WeComCompatibilityPolicy.policy
        val target = target()

        assertFailsWith<IllegalArgumentException> {
            policy.validateTargets(listOf(target.copy(hooks = target.hooks.map {
                it.copy(safeForForwardInference = true)
            })))
        }
        assertFailsWith<IllegalArgumentException> {
            policy.validateTargets(listOf(target.copy(hooks = target.hooks.map {
                it.copy(stringAnchor = "unrelated")
            })))
        }
    }

    @Test
    fun rejectsUnofficialAndQueryBearingSourceUrls() {
        val policy = WeComCompatibilityPolicy.policy

        assertFailsWith<IllegalArgumentException> {
            policy.validateSourceUrl("https://example.org/WeCom.apk")
        }
        assertFailsWith<IllegalArgumentException> {
            policy.validateSourceUrl("https://dldir1.qq.com/wework/work_weixin/WeCom.apk?mirror=1")
        }
    }

    private fun target() = CompatibilityTarget(
        identity = BuildIdentity(
            packageName = "com.tencent.wework",
            versionName = "5.0.9",
            versionCode = 75141,
            abi = "arm64-v8a",
            apkSha256 = APK_SHA256,
            signerSha256 = SIGNER_SHA256,
        ),
        featureRulesVersion = 1,
        hooks = listOf(
            descriptorRule("device_type", "Lcom/tencent/wework/foundation/logic/Application;->getDeviceTypeFromLocal()I", "I"),
            descriptorRule("app_pad", "Lcom/tencent/wework/foundation/logic/Application;->isAndroidPad()Z", "Z"),
            descriptorRule("service_pad", "Lcom/tencent/wework/foundation/impl/WeworkServiceImpl;->isAndroidPad()Z", "Z"),
        ),
        sourceUrl = null,
        verificationStatus = VerificationStatus.RUNTIME_VERIFIED_LOCAL,
    )

    private fun descriptorRule(id: String, descriptor: String, returnDescriptor: String) = HookRule(
        id = id,
        locator = HookLocator.EXACT_DESCRIPTOR,
        parameterDescriptors = emptyList(),
        returnDescriptor = returnDescriptor,
        expectedDescriptor = descriptor,
    )

    private companion object {
        const val APK_SHA256 = "6c4da56f2bc2d75ee97346d40225b0892054527b3baece7895550bc2d0bab801"
        const val APK_508_SHA256 = "59b3e620a3cf1d1f8740ea320e05fd821bb811040b36f69aa307e7fc8a2ca26d"
        const val SIGNER_SHA256 = "66896ae0aae48b9e963a3e034fa2ca813ca2f661f53619a92263a8a5c2e3f388"
        const val OFFICIAL_APK_URL = "https://dldir1.qq.com/wework/work_weixin/WeCom_android_5.0.11.81206_arm64_100038.apk"
    }
}
