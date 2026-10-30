package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.rules.CompletePlan
import io.github.nku100.impad.compat.rules.CompletePlanSelection
import io.github.nku100.impad.rules.CodeInputChangedException
import io.github.nku100.impad.rules.EffectiveCodeRuleSession
import io.github.nku100.impad.rules.RecognitionContext
import io.github.nku100.impad.rules.SemanticRuleCatalog
import io.github.nku100.impad.rules.SemanticRuleResolver
import io.github.nku100.impad.rules.StartupCodeSnapshot
import java.io.File
import java.lang.reflect.Method
import java.security.MessageDigest

sealed interface SemanticFreshResolution {
    data class Selected(val cached: CachedSemanticPlan) : SemanticFreshResolution
    data class Rejected(val outcome: CompletePlanSelection) : SemanticFreshResolution
}

sealed interface SemanticRuntimeOutcome {
    data class Installed(val plan: CompletePlan, val batch: AtomicHookBatch, val cacheKey: String, val cacheHit: Boolean, val attempts: Int) : SemanticRuntimeOutcome
    data class Rejected(val outcome: CompletePlanSelection, val attempts: Int) : SemanticRuntimeOutcome
    data class Failed(val reason: String, val restartRequired: Boolean, val attempts: Int) : SemanticRuntimeOutcome
    data class InputsChangedRepeatedly(val attempts: Int) : SemanticRuntimeOutcome
    data class RestartRequired(val reason: String) : SemanticRuntimeOutcome
}

class SemanticRuntimeCoordinator(
    private val catalog: SemanticRuleCatalog,
    private val cache: FilePlanCache,
    private val executionIdentitySha256: String,
    private val registrar: HookRegistrar,
    private val prepareHooks: (CompletePlan, Map<String, Method>) -> List<HookSpec>,
    private val observe: (ClassLoader) -> ObservedCodeInputs = StartupCodeInputObserver()::observe,
    private val resolveFresh: (InstalledBuild, ObservedCodeInputs, File) -> SemanticFreshResolution = { build, inputs, storage ->
        resolveFreshSnapshot(build, inputs, storage, catalog)
    },
    private val installedInputsStillCurrent: (InstalledBuild) -> Boolean = ::installedApksStillCurrent,
    private val onCacheFailure: (Exception) -> Unit = {},
) {
    private var activeStartup: ActiveStartup? = null
    private var restartRequired = false

    @Synchronized
    fun install(build: InstalledBuild, readyLoader: ClassLoader, storageDirectory: File): SemanticRuntimeOutcome {
        if (restartRequired) return retireForNewGeneration()
        activeStartup?.let { active ->
            val stillCurrent = try { active.inputsStillCurrent() } catch (_: Exception) { false }
            return if (active.readyLoader === readyLoader && active.identity == build.identity &&
                active.apkFileSha256 == build.apkFileSha256 && stillCurrent) active.outcome
            else retireForNewGeneration()
        }
        for (attempt in 1..2) {
            try {
                catalog.policy.validateIdentity(build.identity)
                val inputs = observe(readyLoader)
                val key = SemanticPlanCacheKey.create(build, catalog, inputs.fingerprint, executionIdentitySha256)
                fun current() = inputs.inputsStillCurrent() && installedInputsStillCurrent(build)
                if (!current()) continue
                val cached = if (attempt == 1) cacheRead(key)?.let { CachedSemanticPlan.decode(it, inputs, catalog) } else null
                val cacheHit = cached != null
                val record = cached ?: when (val fresh = resolveFresh(build, inputs, storageDirectory)) {
                    is SemanticFreshResolution.Selected -> CachedSemanticPlan.create(inputs, fresh.cached.snapshot,
                        fresh.cached.archives, fresh.cached.plan, catalog)
                    is SemanticFreshResolution.Rejected -> {
                        if (!current()) continue
                        return SemanticRuntimeOutcome.Rejected(fresh.outcome, attempt)
                    }
                }
                if (!current()) continue
                val hooks = try {
                    val methods = SemanticPlanBinder.bind(record.plan, record.snapshot, inputs.layout, catalog)
                    prepareHooks(record.plan, methods).also { specs ->
                        require(specs.map { it.method } == record.plan.hooks.map { methods.getValue(it.role) }) { "Hook batch differs from the ordered complete plan" }
                    }
                } catch (error: Exception) {
                    if (!cacheHit) throw error
                    cacheInvalidate(key)
                    continue
                }
                if (!current()) continue
                when (val installed = AtomicHookBatch.install(hooks, registrar, ::current)) {
                    is AtomicBatchOutcome.Installed -> {
                        val outcome = SemanticRuntimeOutcome.Installed(record.plan, installed.batch, key, cacheHit, attempt)
                        activeStartup = ActiveStartup(readyLoader, build.identity, build.apkFileSha256.toList(), ::current, outcome)
                        if (!cacheHit) cacheWrite(key, record.encode())
                        return outcome
                    }
                    AtomicBatchOutcome.InputsChanged -> continue
                    is AtomicBatchOutcome.Failed -> {
                        restartRequired = installed.restartRequired
                        return SemanticRuntimeOutcome.Failed(installed.reason, installed.restartRequired, attempt)
                    }
                }
            } catch (_: CodeInputChangedException) {
                continue
            } catch (error: Exception) {
                return SemanticRuntimeOutcome.Failed(error.message ?: error.javaClass.simpleName, false, attempt)
            }
        }
        return SemanticRuntimeOutcome.InputsChangedRepeatedly(2)
    }

    @Synchronized
    fun retireForNewGeneration(): SemanticRuntimeOutcome.RestartRequired {
        restartRequired = true
        val failures = activeStartup?.outcome?.batch?.disableAndUnhook().orEmpty()
        activeStartup = null
        return SemanticRuntimeOutcome.RestartRequired(if (failures.isEmpty()) "Startup code generation changed"
            else "Startup code generation changed; rollback failed: ${failures.joinToString()}")
    }

    private fun cacheRead(key: String): String? = try { cache.read(catalog.appId, key) }
        catch (error: Exception) { cacheFailure(error); null }
    private fun cacheWrite(key: String, payload: String) {
        try { cache.write(catalog.appId, key, payload) } catch (error: Exception) { cacheFailure(error) }
    }
    private fun cacheInvalidate(key: String) {
        try { cache.invalidate(catalog.appId, key) } catch (error: Exception) { cacheFailure(error) }
    }
    private fun cacheFailure(error: Exception) { try { onCacheFailure(error) } catch (_: Exception) { } }

    private class ActiveStartup(
        val readyLoader: ClassLoader,
        val identity: io.github.nku100.impad.compat.BuildIdentity,
        val apkFileSha256: List<String>,
        val inputsStillCurrent: () -> Boolean,
        val outcome: SemanticRuntimeOutcome.Installed,
    )
}

private fun resolveFreshSnapshot(build: InstalledBuild, inputs: ObservedCodeInputs, storage: File, catalog: SemanticRuleCatalog): SemanticFreshResolution {
    java.nio.file.Files.createDirectories(storage.toPath())
    StartupCodeSnapshot().capture(inputs.layout.readyLoaderId, inputs.layout.loaders, storage).use { snapshot ->
        val expected = inputs.containersByLoader
        val manifestsMatch = snapshot.archiveManifests.all { manifest ->
            val source = expected[manifest.loaderId]?.getOrNull(manifest.containerIndex)
            source != null && source.kind == manifest.kind && source.sha256.equals(manifest.containerSha256, ignoreCase = true)
        }
        if (!manifestsMatch || !inputs.inputsStillCurrent()) throw StartupCodeChangedException("Captured bytes differ from the startup observation")
        return EffectiveCodeRuleSession.open(snapshot).use { session ->
            val run = SemanticRuleResolver.resolveDetailed(build.identity, catalog, RecognitionContext(session, snapshot, catalog))
            val plan = (run.outcome as? CompletePlanSelection.Selected)?.match?.plan
            if (plan == null) SemanticFreshResolution.Rejected(run.outcome)
            else SemanticFreshResolution.Selected(CachedSemanticPlan.create(inputs, snapshot.metadata, snapshot.archiveManifests, plan, catalog))
        }
    }
}

private fun installedApksStillCurrent(build: InstalledBuild): Boolean = try {
    build.apkFiles.size == build.apkFileSha256.size && build.apkFiles.zip(build.apkFileSha256).all { (file, expected) ->
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().buffered().use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        digest.digest().joinToString("") { "%02x".format(it) }.equals(expected, ignoreCase = true)
    }
} catch (_: Exception) { false }
