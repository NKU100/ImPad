package io.github.nku100.impad.apps.qq

import io.github.nku100.impad.runtime.*
import org.junit.jupiter.api.Test
import kotlin.test.*

class QqAppAdapterTest {
    class HostContext(private val loader: ClassLoader) : android.content.ContextWrapper(null) {
        override fun getClassLoader(): ClassLoader = loader
    }

    @Test
    fun installsFromTheContextLoaderAfterAttachReturns() {
        val resolved = mutableListOf<String>()
        var attached = false
        val hostLoader = object : ClassLoader(javaClass.classLoader) {
            override fun loadClass(name: String): Class<*> {
                assertTrue(attached)
                resolved += name
                return super.loadClass(name)
            }
        }
        val calls = mutableMapOf<String, HookCallback>()
        var unhooked = false
        val registrar = HookRegistrar { _, id, callback ->
            calls[id] = callback
            HookRegistration { unhooked = true }
        }
        val fixture = QqHookInstallerTest.Fixture::class.java.name.replace('.', '/')
        val device = QqHookInstallerTest.Device::class.java.name.replace('.', '/')
        QqAppAdapter().install(javaClass.classLoader!!,
            mapOf("device_type" to "L$fixture;->device(Landroid/content/Context;)L$device;"),
            registrar, ModuleLog { _, _, _ -> })
        val result = calls.getValue("impad_qq_classloader_bootstrap").intercept(
            HookCall(null, arrayOf(HostContext(hostLoader))) { attached = true; "original" })
        assertEquals("original", result)
        assertTrue(unhooked)
        assertTrue(QqHookInstallerTest.Fixture::class.java.name in resolved)
        assertEquals(QqHookInstallerTest.Device.TABLET,
            calls.getValue("impad_qq_device_type").intercept(HookCall(null, emptyArray()) { error("original getter") }))
    }

    @Test
    fun waitsForTinkerBeforeResolvingBusinessClasses() {
        val requestedClasses = mutableListOf<String>()
        val loader = object : ClassLoader(javaClass.classLoader) {
            override fun loadClass(name: String): Class<*> {
                requestedClasses += name
                if (name == "fixture.Business") error("Business class resolved before Tinker")
                return super.loadClass(name)
            }
        }
        val hooks = mutableListOf<String>()
        val methods = mutableListOf<String>()
        QqAppAdapter().install(loader, mapOf("device_type" to "Lfixture/Business;->device()Z"),
            HookRegistrar { method, id, _ -> methods += method.name; hooks += id; HookRegistration {} },
            ModuleLog { _, _, _ -> })
        assertFalse("fixture.Business" in requestedClasses)
        assertEquals(listOf("attachBaseContext"), methods)
        assertEquals(listOf("impad_qq_classloader_bootstrap"), hooks)
    }
}
