package io.github.nku100.impad.runtime

import android.content.ContextWrapper
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

    @Test
    fun monitoredAttachWaitsForOuterReturnAndRetiresOnOneLaterAttach() {
        val events = mutableListOf<String>()
        val loader = javaClass.classLoader!!
        val context = ContextWrapper(null)
        var callback: HookCallback? = null
        var registrationRemoved = false
        var readyCalls = 0
        var laterCalls = 0
        val registrar = HookRegistrar { _, _, hook ->
            callback = hook
            HookRegistration { registrationRemoved = true; events += "removed" }
        }
        PostAttachClassLoaderBridge(registrar).installMonitoredWithContext(
            javaClass.getDeclaredMethod("attach"), "bootstrap",
            classLoaderAfterAttach = { events += "resolve-loader"; loader },
            onReady = { receivedContext, receivedLoader ->
                readyCalls++
                assertSame(context, receivedContext)
                assertSame(loader, receivedLoader)
                events += "ready"
            },
            onLaterReady = { laterCalls++; events += "later-ready" },
        )

        val outerResult = callback!!.intercept(HookCall(this, arrayOf(context)) {
            val innerResult = callback!!.intercept(HookCall(this, arrayOf(context)) {
                events += "inner-original"
                "inner-result"
            })
            assertEquals("inner-result", innerResult)
            assertFalse(registrationRemoved)
            events += "outer-original"
            "outer-result"
        })
        assertEquals("outer-result", outerResult)
        assertFalse(registrationRemoved)
        assertEquals(1, readyCalls)

        val laterResult = callback!!.intercept(HookCall(this, arrayOf(context)) {
            events += "later-original"
            "later-result"
        })
        assertEquals("later-result", laterResult)
        assertEquals(1, laterCalls)
        assertTrue(registrationRemoved)
        assertEquals(
            listOf("inner-original", "outer-original", "resolve-loader", "ready", "later-ready", "removed", "later-original"),
            events,
        )

        repeat(2) {
            assertEquals("duplicate-result", callback!!.intercept(HookCall(this, arrayOf(context)) {
                events += "duplicate-original"
                "duplicate-result"
            }))
        }
        assertEquals(1, laterCalls)
        assertEquals(1, readyCalls)
        assertEquals(2, events.count { it == "duplicate-original" })
    }

    @Test
    fun monitoredDefaultUsesContextClassLoaderAfterFirstOriginalReturns() {
        val events = mutableListOf<String>()
        val loader = javaClass.classLoader!!
        val context = object : ContextWrapper(null) {
            override fun getClassLoader(): ClassLoader = loader
        }
        var callback: HookCallback? = null
        var readyLoader: ClassLoader? = null
        val registrar = HookRegistrar { _, _, hook -> callback = hook; HookRegistration {} }
        PostAttachClassLoaderBridge(registrar).installMonitoredWithContext(
            javaClass.getDeclaredMethod("attach"), "bootstrap",
            onReady = { receivedContext, receivedLoader ->
                assertSame(context, receivedContext)
                readyLoader = receivedLoader
                events += "ready"
            },
            onLaterReady = {},
        )

        callback!!.intercept(HookCall(this, arrayOf(context)) { events += "original"; Unit })
        assertEquals(listOf("original", "ready"), events)
        assertSame(loader, readyLoader)
    }

    @Test
    fun monitoredNotifiesBeforeFatalUnhookFailure() {
        val events = mutableListOf<String>()
        val fatalUnhookError = OutOfMemoryError("unhook failed")
        var callback: HookCallback? = null
        val registrar = HookRegistrar { _, _, hook ->
            callback = hook
            HookRegistration { events += "unhook"; throw fatalUnhookError }
        }
        PostAttachClassLoaderBridge(registrar).installMonitoredWithContext(
            javaClass.getDeclaredMethod("attach"), "bootstrap",
            classLoaderAfterAttach = { javaClass.classLoader!! },
            onReady = { _, _ -> events += "ready" },
            onLaterReady = { events += "later-ready" },
        )
        val context = ContextWrapper(null)
        callback!!.intercept(HookCall(this, arrayOf(context)) { "initial" })

        assertSame(fatalUnhookError, assertFailsWith<OutOfMemoryError> {
            callback!!.intercept(HookCall(this, arrayOf(context)) { events += "later-original"; "later" })
        })
        assertEquals(listOf("ready", "later-ready", "unhook"), events)
    }

    @Test
    fun synchronousRegistrarCallbackPreservesInitializationErrorAndSuppressesCleanupError() {
        val context = ContextWrapper(null)
        val initializationError = IllegalStateException("ready failed")
        val cleanupError = IllegalArgumentException("unhook failed")
        val events = mutableListOf<String>()
        val registrar = HookRegistrar { _, _, hook ->
            try {
                hook.intercept(HookCall(this, arrayOf(context)) { events += "original"; "original-result" })
            } catch (error: Throwable) {
                assertSame(initializationError, error)
            }
            HookRegistration { events += "unhook"; throw cleanupError }
        }

        val error = assertFailsWith<IllegalStateException> {
            PostAttachClassLoaderBridge(registrar).installMonitoredWithContext(
                javaClass.getDeclaredMethod("attach"), "bootstrap",
                classLoaderAfterAttach = { javaClass.classLoader!! },
                onReady = { _, _ -> events += "ready"; throw initializationError },
                onLaterReady = {},
            )
        }

        assertSame(initializationError, error)
        assertEquals(listOf(cleanupError), error.suppressed.toList())
        assertEquals(listOf("original", "ready", "unhook"), events)
    }
}
