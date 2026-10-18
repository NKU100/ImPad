package io.github.nku100.impad.runtime

import org.junit.jupiter.api.Test
import kotlin.test.*

class PostAttachClassLoaderBridgeTest {
    private fun attach() = Unit

    @Test
    fun deliversTheLoaderOnceAfterOriginalAndRemovesTheHookOnce() {
        val events = mutableListOf<String>()
        var callback: HookCallback? = null
        val registrar = HookRegistrar { _, _, hook ->
            callback = hook
            HookRegistration { events += "removed" }
        }
        PostAttachClassLoaderBridge(registrar).install(
            javaClass.getDeclaredMethod("attach"), "bootstrap",
            { events += "loader"; javaClass.classLoader!! },
        ) { events += "ready" }
        repeat(2) {
            assertEquals("original", callback!!.intercept(HookCall(this, emptyArray()) {
                events += "original"
                "original"
            }))
        }
        assertEquals(listOf("original", "loader", "ready", "removed", "original"), events)
    }

    @Test
    fun originalFailureSkipsReadinessAndStillRemovesTheHook() {
        var callback: HookCallback? = null
        var removed = false
        val registrar = HookRegistrar { _, _, hook ->
            callback = hook
            HookRegistration { removed = true }
        }
        PostAttachClassLoaderBridge(registrar).install(
            javaClass.getDeclaredMethod("attach"), "bootstrap", { error("must not read loader") },
        ) { error("must not install") }
        val error = IllegalStateException("original failure")
        assertSame(error, assertFailsWith<IllegalStateException> {
            callback!!.intercept(HookCall(this, emptyArray()) { throw error })
        })
        assertTrue(removed)
    }

    @Test
    fun reentrantAttachDoesNotInstallBeforeTheOuterAttachReturns() {
        val events = mutableListOf<String>()
        var callback: HookCallback? = null
        val registrar = HookRegistrar { _, _, hook ->
            callback = hook
            HookRegistration { events += "removed" }
        }
        PostAttachClassLoaderBridge(registrar).install(
            javaClass.getDeclaredMethod("attach"), "bootstrap", { javaClass.classLoader!! },
        ) { events += "ready" }
        callback!!.intercept(HookCall(this, emptyArray()) {
            callback!!.intercept(HookCall(this, emptyArray()) { events += "inner" })
            events += "outer"
        })
        assertEquals(listOf("inner", "outer", "ready", "removed"), events)
    }
}
