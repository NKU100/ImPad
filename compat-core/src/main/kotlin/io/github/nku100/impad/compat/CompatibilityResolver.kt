package io.github.nku100.impad.compat

object CompatibilityResolver {
    fun resolve(
        identity: BuildIdentity,
        verification: IdentityVerification,
        targets: List<CompatibilityTarget>,
        facts: List<DexMethodFact>,
        requiredHookIds: Set<String>,
    ): CompatibilityResult {
        val sameBuild = targets.filter {
            it.identity.versionCode == identity.versionCode
        }
        val matchingBuild = sameBuild.filter {
            it.identity.packageName == identity.packageName && it.identity.abi == identity.abi
        }
        if (matchingBuild.isEmpty()) {
            return if (sameBuild.isEmpty()) {
                rejected(CompatibilityStatus.UNKNOWN_BUILD, "No target registered for this app build")
            } else {
                rejected(CompatibilityStatus.IDENTITY_MISMATCH, "Package or ABI does not match the target")
            }
        }

        val expectedTargets = when (verification) {
            IdentityVerification.STATIC_APK -> matchingBuild.filter { target ->
                identity.apkSha256 != null && target.identity.apkSha256.equals(identity.apkSha256, ignoreCase = true)
            }

            IdentityVerification.INSTALLED_PACKAGE -> if (identity.apkSha256 == null) {
                matchingBuild
            } else {
                matchingBuild.filter { target ->
                    target.identity.apkSha256.equals(identity.apkSha256, ignoreCase = true)
                }
            }
        }
        if (expectedTargets.isEmpty()) {
            return rejected(CompatibilityStatus.IDENTITY_MISMATCH, "APK SHA-256 does not match a target profile")
        }
        if (expectedTargets.size > 1) {
            return rejected(CompatibilityStatus.IDENTITY_MISMATCH, "APK SHA-256 is required to distinguish registered variants")
        }
        val target = expectedTargets.single()

        val expectedIdentity = target.identity
        if (!matchesTrustedIdentity(identity, expectedIdentity)) {
            return rejected(CompatibilityStatus.IDENTITY_MISMATCH, "Package, ABI, or signer does not match the target")
        }

        val hookIds = target.hooks.map(HookRule::id)
        if (requiredHookIds.isEmpty() || hookIds.size != requiredHookIds.size || hookIds.toSet() != requiredHookIds ||
            hookIds.any(String::isBlank) || hookIds.size != hookIds.toSet().size
        ) {
            return rejected(
                CompatibilityStatus.INVALID_PROFILE,
                "Profile must define unique, non-empty hook identifiers",
            )
        }

        val resolved = linkedMapOf<String, String>()
        for (rule in target.hooks) {
            val match = HookMethodMatcher.match(rule, facts)
            when (match.status) {
                HookMethodMatchStatus.MISSING_ANCHOR ->
                    return rejected(CompatibilityStatus.MISSING_ANCHOR, "Hook '${rule.id}' anchor was not found")
                HookMethodMatchStatus.MISSING_DESCRIPTOR ->
                    return rejected(CompatibilityStatus.MISSING_METHOD, "Hook '${rule.id}' descriptor was not found")
                HookMethodMatchStatus.SIGNATURE_MISMATCH ->
                    return rejected(CompatibilityStatus.SIGNATURE_MISMATCH, "Hook '${rule.id}' method shape changed")
                HookMethodMatchStatus.AMBIGUOUS_MATCH ->
                    return rejected(CompatibilityStatus.AMBIGUOUS_MATCH, "Hook '${rule.id}' matched multiple methods")
                HookMethodMatchStatus.UNIQUE_MATCH -> Unit
            }

            val method = match.shapeMatchedMethods.single()
            if (method.descriptor != rule.expectedDescriptor) {
                return rejected(CompatibilityStatus.DESCRIPTOR_MISMATCH, "Hook '${rule.id}' descriptor changed")
            }
            resolved[rule.id] = method.descriptor
        }

        return CompatibilityResult(
            status = CompatibilityStatus.COMPATIBLE,
            reason = "All ${target.hooks.size} hook methods resolved uniquely",
            resolvedDescriptors = resolved,
        )
    }

    fun resolveStaticCandidate(
        identity: BuildIdentity,
        targets: List<CompatibilityTarget>,
        facts: List<DexMethodFact>,
        requiredHookIds: Set<String>,
    ): CompatibilityResult {
        val matchingTargets = targets.filter {
            it.identity.versionCode == identity.versionCode &&
                it.identity.packageName == identity.packageName &&
                it.identity.abi == identity.abi && identity.apkSha256 != null &&
                it.identity.apkSha256.equals(identity.apkSha256, ignoreCase = true)
        }
        if (matchingTargets.size > 1) {
            return rejected(CompatibilityStatus.INVALID_PROFILE, "Multiple profiles register this app build")
        }
        if (matchingTargets.size == 1) {
            return resolve(identity, IdentityVerification.STATIC_APK, targets, facts, requiredHookIds)
        }

        val appTargets = targets.filter {
            it.identity.packageName == identity.packageName && it.identity.abi == identity.abi
        }
        if (appTargets.isEmpty()) {
            return rejected(CompatibilityStatus.IDENTITY_MISMATCH, "No compatibility target matches this app package and ABI")
        }
        if (appTargets.none { it.identity.signerSha256.equals(identity.signerSha256, ignoreCase = true) }) {
            return rejected(
                CompatibilityStatus.IDENTITY_MISMATCH,
                "Package, ABI, or signer does not match the registered app policy",
            )
        }

        return rejected(
            CompatibilityStatus.UNKNOWN_BUILD,
            "No target is registered for this APK SHA-256; package, ABI, and signer match the registered app policy",
        )
    }

    private fun matchesTrustedIdentity(identity: BuildIdentity, trustedIdentity: BuildIdentity): Boolean =
        identity.packageName == trustedIdentity.packageName &&
            identity.abi == trustedIdentity.abi &&
            identity.signerSha256.equals(trustedIdentity.signerSha256, ignoreCase = true)

    private fun rejected(status: CompatibilityStatus, reason: String) =
        CompatibilityResult(status = status, reason = reason)
}
