package io.github.nku100.impad.runtime

import java.io.File
import io.github.nku100.impad.compat.BuildIdentity
import io.github.nku100.impad.compat.AppCompatibilityPolicy
import io.github.nku100.impad.compat.COMPATIBILITY_RESOLVER_VERSION
import io.github.nku100.impad.compat.CompatibilityResolver
import io.github.nku100.impad.compat.CompatibilityResult
import io.github.nku100.impad.compat.CompatibilityStatus
import io.github.nku100.impad.compat.CompatibilityTarget
import io.github.nku100.impad.compat.DexFactReader
import io.github.nku100.impad.compat.DexMethodFact
import io.github.nku100.impad.compat.IdentityVerification
import io.github.nku100.impad.compat.HookRule
import io.github.nku100.impad.compat.ResolutionCacheKey
import io.github.nku100.impad.compat.VerificationStatus

data class RuntimeResolution(
    val target: CompatibilityTarget?,
    val result: CompatibilityResult,
    val cacheHit: Boolean,
)

class RuntimeCompatibilityResolver(
    targets: List<CompatibilityTarget>,
    private val policy: AppCompatibilityPolicy,
    private val cache: RuntimeResolutionCache,
    private val hashApk: (File) -> String = InstalledBuildIdentityReader()::sha256,
    private val readFacts: (List<File>, List<HookRule>) -> List<DexMethodFact> = DexFactReader::scan,
    targetValidator: (List<CompatibilityTarget>) -> Unit = policy::validateTargets,
) {
    init {
        targetValidator(targets)
    }

    private val targets = targets.filter {
        it.verificationStatus == VerificationStatus.STATIC_VERIFIED ||
            it.verificationStatus.runtimeVerified
    }

    fun resolve(build: InstalledBuild): RuntimeResolution {
        val apkSha256 = hashApk(build.apkFiles.first())
        val target = targets.singleOrNull {
            it.identity.packageName == build.identity.packageName && it.identity.abi == build.identity.abi &&
                it.identity.apkSha256.equals(apkSha256, ignoreCase = true)
        } ?: return RuntimeResolution(
            target = null,
            result = CompatibilityResult(
                status = CompatibilityStatus.UNKNOWN_BUILD,
                reason = "Installed APK SHA-256 is not registered",
            ),
            cacheHit = false,
        )

        val cached = try {
            cache.read(build.installFingerprint)
        } catch (_: Exception) {
            null
        }
        if (cached?.key == target.cacheKey()) {
            val expectedDescriptors = target.hooks.associate { it.id to it.expectedDescriptor }
            if (cached.result.resolvedDescriptors == expectedDescriptors) {
                return RuntimeResolution(target, cached.result, cacheHit = true)
            }
        }

        val identity = target.identity.forInstalledBuild(build, apkSha256)

        val facts = readFacts(build.apkFiles, target.hooks)
        val result = CompatibilityResolver.resolve(
            identity = identity,
            verification = IdentityVerification.INSTALLED_PACKAGE,
            targets = targets,
            facts = facts,
            requiredHookIds = policy.requiredHookIds,
        )
        if (result.status == CompatibilityStatus.COMPATIBLE) {
            try {
                cache.write(build.installFingerprint, target.cacheKey(), result)
            } catch (_: Exception) {
                // A cache write failure must not prevent compatible hooks from being installed.
            }
        }
        return RuntimeResolution(target, result, cacheHit = false)
    }

    private fun CompatibilityTarget.cacheKey() = ResolutionCacheKey(
        apkSha256 = checkNotNull(identity.apkSha256),
        signerSha256 = identity.signerSha256,
        versionCode = identity.versionCode,
        resolverVersion = COMPATIBILITY_RESOLVER_VERSION,
        featureRulesVersion = featureRulesVersion,
    )

    private fun BuildIdentity.forInstalledBuild(
        build: InstalledBuild,
        apkSha256: String? = this.apkSha256,
    ) = copy(
        packageName = build.identity.packageName,
        abi = build.identity.abi,
        apkSha256 = apkSha256,
    )

}
