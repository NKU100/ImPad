package io.github.nku100.impad.runtime

import kotlin.test.*

class AtomicHookBatchTest {
    private val method = Fixture::class.java.getDeclaredMethod("value")
    private fun specs() = listOf("first", "second", "third").map { HookSpec(method, it, HookCallback { "changed" }) }
    private fun call(callback: HookCallback): Any? = callback.intercept(HookCall(null, emptyArray()) { "original" })

    @Test
    fun noCallbackRunsBusinessBehaviorUntilWholeBatchPassesFinalCheck() {
        val seen = mutableListOf<HookCallback>()
        val duringRegistration = mutableListOf<Any?>()
        val registrar = HookRegistrar { _, _, callback ->
            seen += callback
            duringRegistration.addAll(seen.map(::call))
            HookRegistration { }
        }
        val outcome = AtomicHookBatch.install(specs(), registrar) {
            assertEquals(listOf("original", "original", "original"), seen.map(::call)); true
        }
        val batch = assertIs<AtomicBatchOutcome.Installed>(outcome).batch
        assertTrue(duringRegistration.all { it == "original" })
        assertEquals(listOf("changed", "changed", "changed"), seen.map(::call))
        assertTrue(batch.disableAndUnhook().isEmpty())
        assertEquals(listOf("original", "original", "original"), seen.map(::call))
    }

    @Test
    fun registrationFailureDisablesCallbacksAndRollsBackInReverseOrder() {
        val callbacks = mutableListOf<HookCallback>()
        val removed = mutableListOf<String>()
        val outcome = AtomicHookBatch.install(specs(), HookRegistrar { _, id, callback ->
            if (id == "third") error("registration failed")
            callbacks += callback
            HookRegistration { removed += id; assertEquals("original", call(callback)) }
        }) { true }
        val failure = assertIs<AtomicBatchOutcome.Failed>(outcome)
        assertFalse(failure.restartRequired)
        assertEquals(listOf("second", "first"), removed)
        assertTrue(callbacks.all { call(it) == "original" })
    }

    @Test
    fun inputChangeRejectsWholeBatchAndRollbackFailureRequiresRestart() {
        for (rollbackFails in listOf(false, true)) {
            val callbacks = mutableListOf<HookCallback>()
            var removals = 0
            val outcome = AtomicHookBatch.install(specs(), HookRegistrar { _, _, callback ->
                callbacks += callback
                HookRegistration { removals++; if (rollbackFails) error("unhook failed") }
            }) { false }
            if (rollbackFails) assertTrue(assertIs<AtomicBatchOutcome.Failed>(outcome).restartRequired)
            else assertIs<AtomicBatchOutcome.InputsChanged>(outcome)
            assertEquals(3, removals)
            assertTrue(callbacks.all { call(it) == "original" })
        }
    }

    @Test
    fun retiringBatchIsIdempotentAndReportsUnremovedRegistrations() {
        var unhooks = 0
        lateinit var callback: HookCallback
        val installed = assertIs<AtomicBatchOutcome.Installed>(AtomicHookBatch.install(listOf(specs().first()), HookRegistrar { _, _, hook ->
            callback = hook
            HookRegistration { unhooks++; error("cannot unhook") }
        }) { true })
        assertEquals("changed", call(callback))
        assertEquals(listOf("cannot unhook"), installed.batch.disableAndUnhook())
        assertEquals(listOf("cannot unhook"), installed.batch.disableAndUnhook())
        assertEquals(1, unhooks)
        assertEquals("original", call(callback))
    }

    @Test
    fun invalidBatchNeverRegistersHooks() {
        var calls = 0
        assertIs<AtomicBatchOutcome.Failed>(AtomicHookBatch.install(listOf(specs().first(), specs().first()), HookRegistrar { _, _, _ ->
            calls++; HookRegistration { }
        }) { true })
        assertEquals(0, calls)
    }
    class Fixture { fun value() = "original" }
}
