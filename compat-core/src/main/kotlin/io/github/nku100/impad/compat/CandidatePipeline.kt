package io.github.nku100.impad.compat

import kotlinx.serialization.Serializable

@Serializable
enum class CandidatePipelineStatus {
    DISCOVERED,
    APK_VERIFIED,
    ADAPTATION_RUNNING,
    STATIC_VERIFIED_PENDING_RUNTIME,
    WAITING_RUNTIME,
    RUNTIME_VERIFIED,
    FORMALLY_SUPPORTED,
    FETCH_FAILED,
    IDENTITY_REJECTED,
    BASELINE_INVALID,
    NEEDS_HOOK_REVIEW,
    STATIC_REGRESSION_FAILED,
    RUNTIME_REJECTED,
}

enum class CandidatePipelineEvent {
    APK_FETCHED,
    DOWNLOAD_FAILED,
    IDENTITY_ACCEPTED,
    IDENTITY_REJECTED,
    BASELINE_ACCEPTED,
    BASELINE_REJECTED,
    HOOK_ANALYSIS_UNRESOLVED,
    STATIC_REGRESSION_PASSED,
    STATIC_REGRESSION_FAILED,
    DRAFT_PR_CREATED,
    RUNTIME_PASSED,
    REGISTERED_RUNTIME_VERIFICATION_CONFIRMED,
    RUNTIME_FAILED,
    PULL_REQUEST_MERGED,
}

object CandidatePipelineStateMachine {
    fun transition(
        current: CandidatePipelineStatus,
        event: CandidatePipelineEvent,
    ): CandidatePipelineStatus = when (current to event) {
        CandidatePipelineStatus.DISCOVERED to CandidatePipelineEvent.APK_FETCHED ->
            CandidatePipelineStatus.APK_VERIFIED
        CandidatePipelineStatus.DISCOVERED to CandidatePipelineEvent.DOWNLOAD_FAILED ->
            CandidatePipelineStatus.FETCH_FAILED
        CandidatePipelineStatus.APK_VERIFIED to CandidatePipelineEvent.IDENTITY_ACCEPTED ->
            CandidatePipelineStatus.ADAPTATION_RUNNING
        CandidatePipelineStatus.APK_VERIFIED to CandidatePipelineEvent.IDENTITY_REJECTED ->
            CandidatePipelineStatus.IDENTITY_REJECTED
        CandidatePipelineStatus.ADAPTATION_RUNNING to CandidatePipelineEvent.BASELINE_ACCEPTED ->
            CandidatePipelineStatus.ADAPTATION_RUNNING
        CandidatePipelineStatus.ADAPTATION_RUNNING to CandidatePipelineEvent.BASELINE_REJECTED ->
            CandidatePipelineStatus.BASELINE_INVALID
        CandidatePipelineStatus.ADAPTATION_RUNNING to CandidatePipelineEvent.HOOK_ANALYSIS_UNRESOLVED ->
            CandidatePipelineStatus.NEEDS_HOOK_REVIEW
        CandidatePipelineStatus.ADAPTATION_RUNNING to CandidatePipelineEvent.STATIC_REGRESSION_PASSED ->
            CandidatePipelineStatus.STATIC_VERIFIED_PENDING_RUNTIME
        CandidatePipelineStatus.ADAPTATION_RUNNING to CandidatePipelineEvent.STATIC_REGRESSION_FAILED ->
            CandidatePipelineStatus.STATIC_REGRESSION_FAILED
        CandidatePipelineStatus.STATIC_VERIFIED_PENDING_RUNTIME to CandidatePipelineEvent.DRAFT_PR_CREATED ->
            CandidatePipelineStatus.WAITING_RUNTIME
        CandidatePipelineStatus.WAITING_RUNTIME to CandidatePipelineEvent.RUNTIME_PASSED ->
            CandidatePipelineStatus.RUNTIME_VERIFIED
        CandidatePipelineStatus.STATIC_VERIFIED_PENDING_RUNTIME to CandidatePipelineEvent.REGISTERED_RUNTIME_VERIFICATION_CONFIRMED ->
            CandidatePipelineStatus.RUNTIME_VERIFIED
        CandidatePipelineStatus.WAITING_RUNTIME to CandidatePipelineEvent.RUNTIME_FAILED ->
            CandidatePipelineStatus.RUNTIME_REJECTED
        CandidatePipelineStatus.RUNTIME_VERIFIED to CandidatePipelineEvent.PULL_REQUEST_MERGED ->
            CandidatePipelineStatus.FORMALLY_SUPPORTED
        else -> error("Illegal candidate pipeline transition: $current + $event")
    }
}

@Serializable
enum class CandidateHookStatus {
    UNIQUE_MATCH,
    UNSAFE_ANCHOR,
    MISSING_ANCHOR,
    MISSING_DESCRIPTOR,
    SIGNATURE_MISMATCH,
    AMBIGUOUS_MATCH,
    DESCRIPTOR_MISMATCH,
}

@Serializable
data class CandidateHookDiagnostic(
    val hookId: String,
    val stringAnchor: String?,
    val status: CandidateHookStatus,
    val candidateDescriptors: List<String> = emptyList(),
    val selectedDescriptor: String? = null,
    val message: String,
)

@Serializable
data class CandidateCompatibilityReport(
    val appId: String = "wechat",
    val identity: BuildIdentity?,
    val sourceUrl: String?,
    val baselineVersion: String?,
    val checkedVersions: List<String>,
    val status: CandidatePipelineStatus,
    val hooks: List<CandidateHookDiagnostic> = emptyList(),
    val blockers: List<String> = emptyList(),
    val suggestedProfile: CompatibilityTarget? = null,
)

object CandidateCompatibilityAnalyzer {
    fun analyze(
        identity: BuildIdentity,
        baseline: CompatibilityTarget,
        requiredHookIds: Set<String>,
        facts: List<DexMethodFact>,
        checkedVersions: List<String>,
        regressionPassed: Boolean,
        sourceUrl: String? = null,
    ): CandidateCompatibilityReport {
        val versionsToReport = (checkedVersions + identity.versionName).distinct()
        var status = CandidatePipelineStatus.DISCOVERED
        status = CandidatePipelineStateMachine.transition(status, CandidatePipelineEvent.APK_FETCHED)

        if (!matchesTrustedIdentity(identity, baseline.identity)) {
            status = CandidatePipelineStateMachine.transition(status, CandidatePipelineEvent.IDENTITY_REJECTED)
            return report(
                identity = identity,
                baseline = baseline,
                checkedVersions = versionsToReport,
                sourceUrl = sourceUrl,
                status = status,
                blockers = listOf("Candidate package, ABI, or signer differs from the baseline profile"),
            )
        }

        status = CandidatePipelineStateMachine.transition(status, CandidatePipelineEvent.IDENTITY_ACCEPTED)
        val baselineProblem = validateBaseline(baseline, identity, versionsToReport, requiredHookIds)
        if (baselineProblem != null) {
            status = CandidatePipelineStateMachine.transition(status, CandidatePipelineEvent.BASELINE_REJECTED)
            return report(
                identity = identity,
                baseline = baseline,
                checkedVersions = versionsToReport,
                sourceUrl = sourceUrl,
                status = status,
                blockers = listOf(baselineProblem),
            )
        }
        status = CandidatePipelineStateMachine.transition(status, CandidatePipelineEvent.BASELINE_ACCEPTED)

        val diagnostics = baseline.hooks.map { rule -> analyzeHook(rule, facts) }
        val unresolved = diagnostics.filter { it.status != CandidateHookStatus.UNIQUE_MATCH }
        if (unresolved.isNotEmpty()) {
            status = CandidatePipelineStateMachine.transition(status, CandidatePipelineEvent.HOOK_ANALYSIS_UNRESOLVED)
            return report(
                identity = identity,
                baseline = baseline,
                checkedVersions = versionsToReport,
                sourceUrl = sourceUrl,
                status = status,
                hooks = diagnostics,
                blockers = unresolved.map { "Hook '${it.hookId}': ${it.message}" },
            )
        }

        val terminalEvent = if (regressionPassed) {
            CandidatePipelineEvent.STATIC_REGRESSION_PASSED
        } else {
            CandidatePipelineEvent.STATIC_REGRESSION_FAILED
        }
        status = CandidatePipelineStateMachine.transition(status, terminalEvent)
        val candidateProfile = if (regressionPassed) {
            baseline.copy(
                identity = identity,
                sourceUrl = sourceUrl,
                hooks = baseline.hooks.map { rule ->
                    val diagnostic = diagnostics.single { it.hookId == rule.id }
                    rule.copy(expectedDescriptor = requireNotNull(diagnostic.selectedDescriptor))
                },
                verificationStatus = VerificationStatus.STATIC_VERIFIED,
            )
        } else {
            null
        }

        return report(
            identity = identity,
            baseline = baseline,
            checkedVersions = versionsToReport,
            sourceUrl = sourceUrl,
            status = status,
            hooks = diagnostics,
            blockers = if (regressionPassed) emptyList() else listOf("At least one registered profile failed static regression"),
            suggestedProfile = candidateProfile,
        )
    }

    fun analyzeRegistered(
        identity: BuildIdentity,
        target: CompatibilityTarget,
        requiredHookIds: Set<String>,
        facts: List<DexMethodFact>,
        checkedVersions: List<String>,
        regressionPassed: Boolean,
        profileMergedToMain: Boolean,
        sourceUrl: String? = null,
    ): CandidateCompatibilityReport {
        val versionsToReport = (checkedVersions + identity.versionName).distinct()
        var status = CandidatePipelineStatus.DISCOVERED
        status = CandidatePipelineStateMachine.transition(status, CandidatePipelineEvent.APK_FETCHED)

        if (!matchesExactStaticIdentity(identity, target.identity)) {
            status = CandidatePipelineStateMachine.transition(status, CandidatePipelineEvent.IDENTITY_REJECTED)
            return report(
                identity = identity,
                baseline = target,
                checkedVersions = versionsToReport,
                sourceUrl = sourceUrl,
                status = status,
                blockers = listOf("Candidate package, version, ABI, signer, or APK hash differs from its registered profile"),
            )
        }

        status = CandidatePipelineStateMachine.transition(status, CandidatePipelineEvent.IDENTITY_ACCEPTED)
        val profileShape = CompatibilityResolver.resolve(
            identity = identity,
            verification = IdentityVerification.STATIC_APK,
            targets = listOf(target),
            facts = emptyList(),
            requiredHookIds = requiredHookIds,
        )
        if (profileShape.status == CompatibilityStatus.INVALID_PROFILE) {
            status = CandidatePipelineStateMachine.transition(status, CandidatePipelineEvent.BASELINE_REJECTED)
            return report(
                identity = identity,
                baseline = target,
                checkedVersions = versionsToReport,
                sourceUrl = sourceUrl,
                status = status,
                blockers = listOf(profileShape.reason),
            )
        }
        status = CandidatePipelineStateMachine.transition(status, CandidatePipelineEvent.BASELINE_ACCEPTED)

        val diagnostics = target.hooks.map { rule -> analyzeRegisteredHook(rule, facts) }
        val checkResult = CompatibilityResolver.resolve(
            identity = identity,
            verification = IdentityVerification.STATIC_APK,
            targets = listOf(target),
            facts = facts,
            requiredHookIds = requiredHookIds,
        )
        if (checkResult.status != CompatibilityStatus.COMPATIBLE) {
            status = CandidatePipelineStateMachine.transition(status, CandidatePipelineEvent.HOOK_ANALYSIS_UNRESOLVED)
            return report(
                identity = identity,
                baseline = target,
                checkedVersions = versionsToReport,
                sourceUrl = sourceUrl,
                status = status,
                hooks = diagnostics,
                blockers = listOf(checkResult.reason),
            )
        }

        if (!regressionPassed) {
            status = CandidatePipelineStateMachine.transition(status, CandidatePipelineEvent.STATIC_REGRESSION_FAILED)
            return report(
                identity = identity,
                baseline = target,
                checkedVersions = versionsToReport,
                sourceUrl = sourceUrl,
                status = status,
                hooks = diagnostics,
                blockers = listOf("At least one registered profile failed static regression"),
            )
        }

        status = CandidatePipelineStateMachine.transition(status, CandidatePipelineEvent.STATIC_REGRESSION_PASSED)
        if (target.verificationStatus.runtimeVerified) {
            status = CandidatePipelineStateMachine.transition(
                status,
                CandidatePipelineEvent.REGISTERED_RUNTIME_VERIFICATION_CONFIRMED,
            )
            if (profileMergedToMain) {
                status = CandidatePipelineStateMachine.transition(status, CandidatePipelineEvent.PULL_REQUEST_MERGED)
            }
        }

        return report(
            identity = identity,
            baseline = target,
            checkedVersions = versionsToReport,
            sourceUrl = sourceUrl,
            status = status,
            hooks = diagnostics,
            suggestedProfile = target,
        )
    }

    private fun analyzeHook(
        rule: HookRule,
        facts: List<DexMethodFact>,
    ): CandidateHookDiagnostic {
        val match = HookMethodMatcher.match(rule, facts)
        if (match.status != HookMethodMatchStatus.UNIQUE_MATCH) {
            val status = when (match.status) {
                HookMethodMatchStatus.MISSING_ANCHOR -> CandidateHookStatus.MISSING_ANCHOR
                HookMethodMatchStatus.MISSING_DESCRIPTOR -> CandidateHookStatus.MISSING_DESCRIPTOR
                HookMethodMatchStatus.SIGNATURE_MISMATCH -> CandidateHookStatus.SIGNATURE_MISMATCH
                HookMethodMatchStatus.AMBIGUOUS_MATCH -> CandidateHookStatus.AMBIGUOUS_MATCH
                HookMethodMatchStatus.UNIQUE_MATCH -> error("Unique match was handled above")
            }
            val candidateMethods = if (match.status == HookMethodMatchStatus.SIGNATURE_MISMATCH) {
                match.anchoredMethods
            } else {
                match.shapeMatchedMethods
            }
            return CandidateHookDiagnostic(
                hookId = rule.id,
                stringAnchor = rule.stringAnchor,
                status = status,
                candidateDescriptors = candidateMethods.map(DexMethodFact::descriptor).distinct().sorted(),
                message = when (match.status) {
                    HookMethodMatchStatus.MISSING_ANCHOR -> "Anchor was not found in candidate DEX"
                    HookMethodMatchStatus.MISSING_DESCRIPTOR -> "Exact descriptor was not found in candidate DEX"
                    HookMethodMatchStatus.SIGNATURE_MISMATCH ->
                        "Anchor exists, but no method has the registered parameter and return shape"
                    HookMethodMatchStatus.AMBIGUOUS_MATCH -> "Anchor and method shape matched multiple methods"
                    HookMethodMatchStatus.UNIQUE_MATCH -> error("Unique match was handled above")
                },
            )
        }

        val descriptor = match.shapeMatchedMethods.single().descriptor
        val descriptors = match.shapeMatchedMethods.map(DexMethodFact::descriptor).distinct().sorted()
        if (!rule.safeForForwardInference) {
            return CandidateHookDiagnostic(
                hookId = rule.id,
                stringAnchor = rule.stringAnchor,
                status = CandidateHookStatus.UNSAFE_ANCHOR,
                candidateDescriptors = descriptors,
                selectedDescriptor = descriptor,
                message = "Profile does not allow forward inference from this locator",
            )
        }

        return CandidateHookDiagnostic(
            hookId = rule.id,
            stringAnchor = rule.stringAnchor,
            status = CandidateHookStatus.UNIQUE_MATCH,
            candidateDescriptors = descriptors,
            selectedDescriptor = descriptor,
            message = "Anchor and method shape matched uniquely",
        )
    }

    private fun analyzeRegisteredHook(
        rule: HookRule,
        facts: List<DexMethodFact>,
    ): CandidateHookDiagnostic {
        val match = HookMethodMatcher.match(rule, facts)
        val method = match.shapeMatchedMethods.singleOrNull()
        val status = when (match.status) {
            HookMethodMatchStatus.MISSING_ANCHOR -> CandidateHookStatus.MISSING_ANCHOR
            HookMethodMatchStatus.MISSING_DESCRIPTOR -> CandidateHookStatus.MISSING_DESCRIPTOR
            HookMethodMatchStatus.SIGNATURE_MISMATCH -> CandidateHookStatus.SIGNATURE_MISMATCH
            HookMethodMatchStatus.AMBIGUOUS_MATCH -> CandidateHookStatus.AMBIGUOUS_MATCH
            HookMethodMatchStatus.UNIQUE_MATCH -> if (method?.descriptor == rule.expectedDescriptor) {
                CandidateHookStatus.UNIQUE_MATCH
            } else {
                CandidateHookStatus.DESCRIPTOR_MISMATCH
            }
        }
        val selectedDescriptor = method?.descriptor
        val candidates = (if (match.status == HookMethodMatchStatus.SIGNATURE_MISMATCH) {
            match.anchoredMethods
        } else {
            match.shapeMatchedMethods
        })
            .map(DexMethodFact::descriptor)
            .distinct()
            .sorted()
        return CandidateHookDiagnostic(
            hookId = rule.id,
            stringAnchor = rule.stringAnchor,
            status = status,
            candidateDescriptors = candidates,
            selectedDescriptor = selectedDescriptor,
            message = when (status) {
                CandidateHookStatus.UNIQUE_MATCH -> "Registered anchor, shape, and descriptor matched uniquely"
                CandidateHookStatus.MISSING_ANCHOR -> "Anchor was not found in candidate DEX"
                CandidateHookStatus.MISSING_DESCRIPTOR -> "Exact descriptor was not found in candidate DEX"
                CandidateHookStatus.SIGNATURE_MISMATCH -> "Anchor exists, but no method has the registered parameter and return shape"
                CandidateHookStatus.AMBIGUOUS_MATCH -> "Anchor and method shape matched multiple methods"
                CandidateHookStatus.DESCRIPTOR_MISMATCH -> "Unique anchored method descriptor differs from the registered profile"
                CandidateHookStatus.UNSAFE_ANCHOR -> error("Registered checks do not evaluate forward-safety metadata")
            },
        )
    }

    private fun validateBaseline(
        baseline: CompatibilityTarget,
        candidate: BuildIdentity,
        checkedVersions: List<String>,
        requiredHookIds: Set<String>,
    ): String? {
        if (!baseline.verificationStatus.runtimeVerified) {
            return "Baseline profile has not completed runtime verification"
        }
        if (baseline.identity.versionCode > candidate.versionCode) {
            return "Baseline version cannot be newer than the candidate"
        }
        if (baseline.identity.versionName !in checkedVersions) {
            return "Adaptation baseline is not included in the static regression window"
        }
        val hookIds = baseline.hooks.map(HookRule::id)
        if (requiredHookIds.isEmpty() || hookIds.size != requiredHookIds.size ||
            hookIds.toSet() != requiredHookIds || hookIds.any(String::isBlank)
        ) {
            return "Baseline hooks do not match the registered app policy"
        }
        if (baseline.hooks.any { rule ->
                rule.expectedDescriptor.isBlank() || when (rule.locator) {
                    HookLocator.STRING_ANCHOR -> rule.stringAnchor.isNullOrBlank()
                    HookLocator.EXACT_DESCRIPTOR -> rule.stringAnchor != null || rule.safeForForwardInference
                }
            }
        ) {
            return "Baseline hook locators and descriptors are invalid"
        }
        return null
    }

    private fun matchesTrustedIdentity(candidate: BuildIdentity, baseline: BuildIdentity): Boolean =
        candidate.packageName == baseline.packageName &&
            candidate.abi == baseline.abi &&
            candidate.signerSha256.equals(baseline.signerSha256, ignoreCase = true)

    private fun matchesExactStaticIdentity(candidate: BuildIdentity, registered: BuildIdentity): Boolean =
        candidate.packageName == registered.packageName &&
            candidate.versionCode == registered.versionCode &&
            candidate.abi == registered.abi &&
            candidate.apkSha256 != null && registered.apkSha256 != null &&
            candidate.apkSha256.equals(registered.apkSha256, ignoreCase = true) &&
            candidate.signerSha256.equals(registered.signerSha256, ignoreCase = true)

    private fun report(
        identity: BuildIdentity,
        baseline: CompatibilityTarget,
        checkedVersions: List<String>,
        sourceUrl: String?,
        status: CandidatePipelineStatus,
        hooks: List<CandidateHookDiagnostic> = emptyList(),
        blockers: List<String> = emptyList(),
        suggestedProfile: CompatibilityTarget? = null,
    ) = CandidateCompatibilityReport(
        identity = identity,
        sourceUrl = sourceUrl,
        baselineVersion = baseline.identity.versionName,
        checkedVersions = checkedVersions,
        status = status,
        hooks = hooks,
        blockers = blockers,
        suggestedProfile = suggestedProfile,
    )
}
