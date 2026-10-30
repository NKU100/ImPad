package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.AppCompatibilityPolicies
import io.github.nku100.impad.compat.BuildIdentity
import io.github.nku100.impad.compat.code.*
import io.github.nku100.impad.compat.rules.*
import io.github.nku100.impad.rules.CodeLoaderInput
import io.github.nku100.impad.rules.SemanticAppCatalogs
import java.io.File
import java.nio.file.Files
import kotlin.test.*

class SemanticRuntimeCoordinatorTest {
    @Test
    fun warmCacheBindsWithoutOpeningFreshAnalysis() = environment { env ->
        var analyses = 0
        val first = env.coordinator(fresh = { analyses++; SemanticFreshResolution.Selected(env.record()) })
        assertIs<SemanticRuntimeOutcome.Installed>(first.install(env.build, env.loader, env.root)).also { assertFalse(it.cacheHit) }
        val second = env.coordinator(fresh = { error("cache hit must not analyze DEX") })
        assertTrue(assertIs<SemanticRuntimeOutcome.Installed>(second.install(env.build, env.loader, env.root)).cacheHit)
        assertEquals(1, analyses)
    }

    @Test
    fun cacheBindingFailureGetsOneFreshAttemptAndNeverAThird() = environment { env ->
        for (freshAlsoInvalid in listOf(false, true)) {
            env.seed(env.record(missing = true))
            var observations = 0
            var analyses = 0
            val coordinator = env.coordinator(observe = { observations++; env.observation() }, fresh = {
                analyses++; SemanticFreshResolution.Selected(env.record(missing = freshAlsoInvalid))
            })
            val outcome = coordinator.install(env.build, env.loader, env.root)
            if (freshAlsoInvalid) assertIs<SemanticRuntimeOutcome.Failed>(outcome)
            else assertEquals(2, assertIs<SemanticRuntimeOutcome.Installed>(outcome).attempts)
            assertEquals(2, observations)
            assertEquals(1, analyses)
        }
    }

    @Test
    fun cacheBindingFailureAndSubsequentContentChangeShareTwoAttemptBudget() = environment { env ->
        env.seed(env.record(missing = true))
        var observations = 0
        var current = true
        var registrations = 0
        val coordinator = env.coordinator(
            observe = { observations++; env.observation { current } },
            fresh = { SemanticFreshResolution.Selected(env.record()) },
            prepare = { methods -> current = false; env.hooks(methods) },
            registrar = HookRegistrar { _, _, _ -> registrations++; HookRegistration { } },
        )
        assertIs<SemanticRuntimeOutcome.InputsChangedRepeatedly>(coordinator.install(env.build, env.loader, env.root))
        assertEquals(2, observations)
        assertEquals(0, registrations)
    }

    @Test
    fun postRegistrationChangeRollsBackBeforeFreshRetry() = environment { env ->
        var observations = 0
        var current = true
        val events = mutableListOf<String>()
        val coordinator = env.coordinator(
            observe = { observations++; current = true; env.observation { current } },
            fresh = { events += "analyze"; SemanticFreshResolution.Selected(env.record()) },
            registrar = HookRegistrar { _, _, _ ->
                events += "register"
                if (observations == 1) current = false
                HookRegistration { events += "unhook" }
            },
        )
        assertEquals(2, assertIs<SemanticRuntimeOutcome.Installed>(coordinator.install(env.build, env.loader, env.root)).attempts)
        assertEquals(listOf("analyze", "register", "unhook", "analyze", "register"), events)
    }

    @Test
    fun conflictAndRegistrationFailureDoNotRetry() = environment { env ->
        var analyses = 0
        val conflict = CompletePlanSelection.Conflict(emptyList())
        val rejected = env.coordinator(fresh = { analyses++; SemanticFreshResolution.Rejected(conflict) })
        assertSame(conflict, assertIs<SemanticRuntimeOutcome.Rejected>(rejected.install(env.build, env.loader, env.root)).outcome)
        assertEquals(1, analyses)
        analyses = 0
        val failed = env.coordinator(fresh = { analyses++; SemanticFreshResolution.Selected(env.record()) }, registrar = HookRegistrar { _, _, _ -> error("registration failure") })
        assertFalse(assertIs<SemanticRuntimeOutcome.Failed>(failed.install(env.build, env.loader, env.root)).restartRequired)
        assertEquals(1, analyses)
    }

    @Test
    fun duplicateStartupRequestKeepsTheInstalledBatchWithoutAnalyzingOrRegisteringAgain() = environment { env ->
        var analyses = 0
        var registrations = 0
        var removed = 0
        var current = true
        lateinit var callback: HookCallback
        val coordinator = env.coordinator(observe = { env.observation { current } },
            fresh = { analyses++; SemanticFreshResolution.Selected(env.record()) },
            registrar = HookRegistrar { _, _, cb ->
                registrations++; callback = cb; HookRegistration { removed++ }
            })
        val installed = assertIs<SemanticRuntimeOutcome.Installed>(coordinator.install(env.build, env.loader, env.root))
        assertSame(installed, coordinator.install(env.build.copy(), env.loader, env.root))
        assertEquals(1, analyses)
        assertEquals(1, registrations)
        assertEquals(0, removed)
        assertEquals("tablet", callback.intercept(HookCall(null, emptyArray()) { "original" }))
        current = false
        assertIs<SemanticRuntimeOutcome.RestartRequired>(coordinator.install(env.build, env.loader, env.root))
        assertEquals(1, removed)
        assertEquals("original", callback.intercept(HookCall(null, emptyArray()) { "original" }))
    }

    @Test
    fun laterGenerationDisablesOldBatchAndPermanentlyRequiresRestart() = environment { env ->
        var removed = 0
        lateinit var callback: HookCallback
        val coordinator = env.coordinator(fresh = { SemanticFreshResolution.Selected(env.record()) }, registrar = HookRegistrar { _, _, cb ->
            callback = cb; HookRegistration { removed++ }
        })
        assertIs<SemanticRuntimeOutcome.Installed>(coordinator.install(env.build, env.loader, env.root))
        fun invoke() = callback.intercept(HookCall(null, emptyArray()) { "original" })
        assertEquals("tablet", invoke())
        assertIs<SemanticRuntimeOutcome.RestartRequired>(coordinator.retireForNewGeneration())
        assertEquals("original", invoke())
        assertEquals(1, removed)
        assertIs<SemanticRuntimeOutcome.RestartRequired>(coordinator.install(env.build, env.loader, env.root))
        assertEquals(1, removed)
    }

    private fun environment(block: (Environment) -> Unit) {
        val root = Files.createTempDirectory("semantic-runtime-test-").toFile()
        try { block(Environment(root)) } finally { root.deleteRecursively() }
    }
    private class Environment(val root: File) {
        val catalog = SemanticAppCatalogs.forApp("qq")
        val loader = Fixture::class.java.classLoader!!
        val hash = "a".repeat(64)
        val build = InstalledBuild(BuildIdentity(catalog.policy.packageName, "test", 14378, "arm64-v8a", hash, AppCompatibilityPolicies.require("qq").signerSha256), listOf(File("base.apk")), "ignored", root, listOf(hash))
        val cache = FilePlanCache(File(root, "cache"))
        val snapshot = CodeSnapshot("root", listOf(CodeLoader("root", null, CodeDelegation.PARENT_FIRST, listOf(hash))))
        fun observation(current: () -> Boolean = { true }) = ObservedCodeInputs(
            StartupLoaderLayout("root", listOf(CodeLoaderInput("root", null, CodeDelegation.PARENT_FIRST, listOf(File("base.apk")))), mapOf("root" to loader)),
            mapOf("root" to listOf(ObservedCodeContainer(File("base.apk"), hash, CodeContainerKind.ZIP))), current,
        )
        fun record(missing: Boolean = false): CachedSemanticPlan {
            val strategy = catalog.strategies.single()
            val role = strategy.roles.single()
            val descriptor = "L${Fixture::class.java.name.replace('.', '/')};->${if (missing) "missing" else "value"}()Z"
            val plan = CompletePlan(snapshot.contentSha256(), catalog.contractId, catalog.contractRevision, strategy.id, strategy.revision,
                listOf(PlanHook(role.role, descriptor, MethodShape(emptyList(), "boolean", false), TargetCodeSource("root", 0, hash), role.phase, role.configuration)))
            return CachedSemanticPlan.create(observation(), snapshot, listOf(CodeArchiveManifest("root", 0, CodeContainerKind.ZIP, hash, listOf(hash))), plan, catalog)
        }
        fun seed(record: CachedSemanticPlan) = cache.write("qq", SemanticPlanCacheKey.create(build, catalog, observation().fingerprint, hash), record.encode())
        fun hooks(methods: Map<String, java.lang.reflect.Method>) = listOf(HookSpec(methods.getValue("device_type"), "test_device_type", HookCallback { "tablet" }))
        fun coordinator(
            observe: (ClassLoader) -> ObservedCodeInputs = { observation() },
            fresh: (ObservedCodeInputs) -> SemanticFreshResolution,
            prepare: (Map<String, java.lang.reflect.Method>) -> List<HookSpec> = ::hooks,
            registrar: HookRegistrar = HookRegistrar { _, _, _ -> HookRegistration { } },
        ) = SemanticRuntimeCoordinator(catalog, cache, hash, registrar,
            prepareHooks = { _, methods -> prepare(methods) }, observe = observe,
            resolveFresh = { _, observed, _ -> fresh(observed) }, installedInputsStillCurrent = { true })
    }
    class Fixture { fun value() = false }
}
