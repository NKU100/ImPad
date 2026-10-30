package io.github.nku100.impad.runtime

import android.content.Context
import android.content.ContextWrapper
import com.tencent.tinker.loader.app.TinkerApplication
import java.lang.reflect.Method
import org.junit.jupiter.api.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertFailsWith
import kotlin.test.assertSame
import kotlin.test.assertTrue

class TinkerClassLoaderBridgeTest {
    @Test
    fun suppliesTheClassLoaderAfterBaseContextAttachedReturns() {
        val postAttachLoader = javaClass.classLoader!!
        val application = TinkerApplication(postAttachLoader)
        var originalCalled = false
        var readyClassLoader: ClassLoader? = null
        var registeredMethod: Method? = null
        var registeredCallback: HookCallback? = null
        var unhooked = false
        val registrar = object : HookRegistrar {
            override fun hook(method: Method, id: String, callback: HookCallback): HookRegistration {
                assertEquals("onBaseContextAttached", method.name)
                registeredMethod = method
                registeredCallback = callback
                return HookRegistration { unhooked = true }
            }
        }

        TinkerClassLoaderBridge(
            registrar = registrar,
            postAttachClassLoader = { (it as TinkerApplication).postAttachClassLoader },
        ).install(javaClass.classLoader!!) { readyClassLoader = it }
        val result = registeredCallback!!.intercept(HookCall(application, emptyArray()) {
            originalCalled = true
            "original"
        })

        assertEquals("onBaseContextAttached", registeredMethod!!.name)
        assertTrue(originalCalled)
        assertTrue(unhooked)
        assertSame(postAttachLoader, readyClassLoader)
        assertEquals("original", result)
    }

    @Test
    fun removesBootstrapHookWhenPostAttachResolutionFails() {
        val application = TinkerApplication(javaClass.classLoader!!)
        var registeredCallback: HookCallback? = null
        var unhooked = false
        val registrar = object : HookRegistrar {
            override fun hook(method: Method, id: String, callback: HookCallback): HookRegistration {
                registeredCallback = callback
                return HookRegistration { unhooked = true }
            }
        }
        TinkerClassLoaderBridge(
            registrar = registrar,
            postAttachClassLoader = { (it as TinkerApplication).postAttachClassLoader },
        ).install(javaClass.classLoader!!) { error("resolution failure") }

        assertFailsWith<IllegalStateException> {
            registeredCallback!!.intercept(HookCall(application, emptyArray()) { "original" })
        }
        assertTrue(unhooked)
    }

    @Test
    fun monitoredApiForwardsTheExplicitPostAttachLoaderAndLaterNotification() {
        val bootstrapLoader = javaClass.classLoader!!
        val postAttachLoader = object : ClassLoader(bootstrapLoader) {}
        val application = TinkerApplication(postAttachLoader)
        val context = ContextWrapper(null)
        val events = mutableListOf<String>()
        var registeredCallback: HookCallback? = null
        var unhooked = false
        val registrar = HookRegistrar { method, _, callback ->
            assertEquals("onBaseContextAttached", method.name)
            registeredCallback = callback
            HookRegistration { unhooked = true; events += "removed" }
        }
        TinkerClassLoaderBridge(
            registrar = registrar,
            postAttachClassLoader = { (it as TinkerApplication).postAttachClassLoader },
        ).installMonitoredWithContext(
            bootstrapLoader,
            onReady = { readyContext, loader ->
                assertSame(context, readyContext)
                assertSame(postAttachLoader, loader)
                events += "ready"
            },
            onLaterReady = { events += "later-ready" },
        )

        assertEquals("first", registeredCallback!!.intercept(HookCall(application, arrayOf(context)) {
            events += "first-original"
            "first"
        }))
        assertFalse(unhooked)
        assertEquals("later", registeredCallback!!.intercept(HookCall(application, arrayOf(context)) {
            events += "later-original"
            "later"
        }))
        assertTrue(unhooked)
        assertEquals(listOf("first-original", "ready", "later-ready", "removed", "later-original"), events)
    }
}
