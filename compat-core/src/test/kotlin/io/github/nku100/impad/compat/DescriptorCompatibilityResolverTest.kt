package io.github.nku100.impad.compat

import io.github.nku100.impad.compat.apps.wecom.WeComCompatibilityPolicy
import kotlin.test.Test
import kotlin.test.assertEquals

class DescriptorCompatibilityResolverTest {
    @Test
    fun resolvesAllExactDescriptorHooksForTheRegisteredWeComBuild() {
        val target = target()
        val result = CompatibilityResolver.resolve(
            identity = target.identity,
            verification = IdentityVerification.STATIC_APK,
            targets = listOf(target),
            facts = facts(),
            requiredHookIds = WeComCompatibilityPolicy.policy.requiredHookIds,
        )

        assertEquals(CompatibilityStatus.COMPATIBLE, result.status)
        assertEquals(target.hooks.associate { it.id to it.expectedDescriptor }, result.resolvedDescriptors)
    }

    @Test
    fun rejectsAnExactDescriptorMissingFromTheDexFacts() {
        val target = target()
        val result = CompatibilityResolver.resolve(
            identity = target.identity,
            verification = IdentityVerification.STATIC_APK,
            targets = listOf(target),
            facts = facts().dropLast(1),
            requiredHookIds = WeComCompatibilityPolicy.policy.requiredHookIds,
        )

        assertEquals(CompatibilityStatus.MISSING_METHOD, result.status)
    }

    @Test
    fun rejectsDescriptorFactsWhenAnyTrustedApkIdentityFieldChanges() {
        val target = target()
        val identities = listOf(
            target.identity.copy(versionName = "5.0.12", versionCode = 81207) to CompatibilityStatus.UNKNOWN_BUILD,
            target.identity.copy(abi = "armeabi-v7a") to CompatibilityStatus.IDENTITY_MISMATCH,
            target.identity.copy(apkSha256 = "a".repeat(64)) to CompatibilityStatus.IDENTITY_MISMATCH,
            target.identity.copy(signerSha256 = "b".repeat(64)) to CompatibilityStatus.IDENTITY_MISMATCH,
        )

        identities.forEach { (identity, expectedStatus) ->
            val result = CompatibilityResolver.resolve(
                identity = identity,
                verification = IdentityVerification.STATIC_APK,
                targets = listOf(target),
                facts = facts(),
                requiredHookIds = WeComCompatibilityPolicy.policy.requiredHookIds,
            )
            assertEquals(expectedStatus, result.status)
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
            rule("device_type", "Lcom/tencent/wework/foundation/logic/Application;->getDeviceTypeFromLocal()I", "I"),
            rule("app_pad", "Lcom/tencent/wework/foundation/logic/Application;->isAndroidPad()Z", "Z"),
            rule("service_pad", "Lcom/tencent/wework/foundation/impl/WeworkServiceImpl;->isAndroidPad()Z", "Z"),
        ),
        sourceUrl = null,
        verificationStatus = VerificationStatus.RUNTIME_VERIFIED_LOCAL,
    )

    private fun rule(id: String, descriptor: String, returnDescriptor: String) = HookRule(
        id = id,
        locator = HookLocator.EXACT_DESCRIPTOR,
        parameterDescriptors = emptyList(),
        returnDescriptor = returnDescriptor,
        expectedDescriptor = descriptor,
    )

    private fun facts(): List<DexMethodFact> = target().hooks.map { rule ->
        DexMethodFact(
            descriptor = rule.expectedDescriptor,
            parameterDescriptors = rule.parameterDescriptors,
            returnDescriptor = rule.returnDescriptor,
            strings = emptySet(),
        )
    }

    private companion object {
        const val APK_SHA256 = "6c4da56f2bc2d75ee97346d40225b0892054527b3baece7895550bc2d0bab801"
        const val SIGNER_SHA256 = "66896ae0aae48b9e963a3e034fa2ca813ca2f661f53619a92263a8a5c2e3f388"
    }
}
