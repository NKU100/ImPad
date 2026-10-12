package io.github.nku100.impad.compat

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class HookRule(
    val id: String,
    val stringAnchor: String? = null,
    val parameterDescriptors: List<String>,
    val returnDescriptor: String,
    val expectedDescriptor: String,
    val safeForForwardInference: Boolean = false,
    val locator: HookLocator = HookLocator.STRING_ANCHOR,
)

@Serializable
enum class HookLocator {
    STRING_ANCHOR,
    EXACT_DESCRIPTOR,
}

@Serializable
data class CompatibilityTarget(
    val identity: BuildIdentity,
    val featureRulesVersion: Int,
    val hooks: List<HookRule>,
    val sourceUrl: String? = null,
    val verificationStatus: VerificationStatus = VerificationStatus.UNVERIFIED,
)

@Serializable
enum class VerificationStatus {
    @SerialName("unverified")
    UNVERIFIED,

    @SerialName("static-verified")
    STATIC_VERIFIED,

    @SerialName("runtime-verified-local")
    RUNTIME_VERIFIED_LOCAL,

    @SerialName("runtime-verified-hosted")
    RUNTIME_VERIFIED_HOSTED;

    val runtimeVerified: Boolean
        get() = this == RUNTIME_VERIFIED_LOCAL || this == RUNTIME_VERIFIED_HOSTED
}
