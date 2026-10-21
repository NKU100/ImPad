package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.SelectionOutcome
import io.github.nku100.impad.compat.rules.VersionRouter
import io.github.nku100.impad.rules.RuleCatalog
import io.github.nku100.impad.rules.RuleExecutionFingerprint
import io.github.nku100.impad.rules.RuleExecutionIdentityReport
import io.github.nku100.impad.rules.RuleSession
import io.github.nku100.impad.rules.SharedRuleResolver
import java.io.File

data class RuleRuntimeResolution(
    val outcome: SelectionOutcome,
    val plan: ResolvedPlan?,
    val cacheKey: RuleResolutionCacheKey,
    val cacheHit: Boolean,
)

class RuleRuntimeResolver(
    private val catalog: RuleCatalog,
    private val cache: RuleResolutionCache,
    private val executionIdentity: RuleExecutionIdentityReport = RuleExecutionFingerprint.createReport(
        catalog.sha256,
        RuleExecutionFingerprint.loadManifest(),
    ),
    private val openSession: (List<File>) -> RuleSession = RuleSession::open,
) {
    fun resolve(build: InstalledBuild): RuleRuntimeResolution {
        catalog.policy.validateIdentity(build.identity)
        require(build.apkFileSha256.size == build.apkFiles.size && build.apkFileSha256.isNotEmpty()) {
            "Rule resolution requires a content hash for every installed APK and split"
        }
        val route = VersionRouter.candidates(build.identity.versionCode, catalog.bindings)
        val key = RuleResolutionCacheKey(
            appId = catalog.appId,
            packageName = build.identity.packageName,
            abi = build.identity.abi,
            signerSha256 = build.identity.signerSha256.lowercase(),
            versionCode = build.identity.versionCode,
            apkInputSha256 = build.apkFileSha256,
            routeRuleIds = route,
            routeStrategyIds = route.map { ruleId ->
                requireNotNull(catalog.definitions.singleOrNull { it.ruleId == ruleId })
                    .strategyId
            },
            ruleCatalogSha256 = catalog.sha256,
            ruleImplementationSha256 = executionIdentity.ruleImplementationSha256,
            resolverImplementationSha256 = executionIdentity.sharedResolverImplementationSha256,
            strategyImplementationSha256 = executionIdentity.strategyImplementationSha256,
            dexkitApiSha256 = executionIdentity.dexkitAndroidApiSha256,
            dexkitNativeSha256 = executionIdentity.dexkitAndroidArm64NativeSha256,
            dexkitSourceCommit = executionIdentity.dexkitSourceCommit,
            dexkitLockSha256 = executionIdentity.dexkitLockSha256,
            executionIdentitySha256 = executionIdentity.cacheIdentitySha256,
        )
        val cached = try {
            cache.read(key)
        } catch (_: Exception) {
            null
        }
        if (cached != null) {
            val validCachedPlan = cached.rule.ruleId in route &&
                catalog.definitions.singleOrNull { it.ref == cached.rule }?.requiredTargetIds == cached.targets.keys &&
                cached.targets.values.all { it.descriptor.isNotBlank() }
            if (validCachedPlan) return RuleRuntimeResolution(
                SelectionOutcome.Selected(cached), cached, key, cacheHit = true,
            )
        }

        val outcome = openSession(build.apkFiles).use { session ->
            SharedRuleResolver.resolve(build.identity, catalog, session)
        }
        val plan = (outcome as? SelectionOutcome.Selected)?.plan
        if (plan != null) {
            try {
                cache.write(key, plan)
            } catch (_: Exception) {
                // A cache write failure must not change the rule selection.
            }
        }
        return RuleRuntimeResolution(outcome, plan, key, cacheHit = false)
    }
}
