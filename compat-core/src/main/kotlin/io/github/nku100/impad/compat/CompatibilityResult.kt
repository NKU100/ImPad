package io.github.nku100.impad.compat

import kotlinx.serialization.Serializable

@Serializable
enum class CompatibilityStatus {
    COMPATIBLE,
    UNKNOWN_BUILD,
    IDENTITY_MISMATCH,
    INVALID_PROFILE,
    MISSING_ANCHOR,
    MISSING_METHOD,
    SIGNATURE_MISMATCH,
    AMBIGUOUS_MATCH,
    DESCRIPTOR_MISMATCH,
}

@Serializable
data class CompatibilityResult(
    val status: CompatibilityStatus,
    val reason: String,
    val resolvedDescriptors: Map<String, String> = emptyMap(),
)
