package io.github.nku100.impad.apps.wecom

import kotlin.test.assertFalse
import kotlin.test.assertTrue
import org.junit.jupiter.api.Test
import io.github.nku100.impad.runtime.*
import kotlin.test.assertEquals

class WeComAppAdapterTest {
    class Targets {
        fun device(): Int = 0
        fun appPad(): Boolean = false
        fun servicePad(): Boolean = false
    }

    @Test
    fun resolvesBusinessClassesOnlyAfterTinkerReturns() {
        var attached = false
        val loader = object : ClassLoader(javaClass.classLoader) {
            override fun loadClass(name: String): Class<*> {
                if (name == Targets::class.java.name) assertTrue(attached)
                return super.loadClass(name)
            }
        }
        val hooks = mutableMapOf<String, HookCallback>()
        val removed = mutableListOf<String>()
        val registrar = HookRegistrar { _, id, callback ->
            hooks[id] = callback
            HookRegistration { removed += id }
        }
        val target = Targets::class.java.name.replace('.', '/')
        WeComAppAdapter().install(loader, mapOf(
            "device_type" to "L$target;->device()I",
            "app_pad" to "L$target;->appPad()Z",
            "service_pad" to "L$target;->servicePad()Z"), registrar, ModuleLog { _, _, _ -> })
        assertEquals(setOf("impad_wecom_tinker_bootstrap"), hooks.keys)
        val application = com.tencent.tinker.loader.app.TinkerApplication(loader)
        hooks.getValue("impad_wecom_tinker_bootstrap").intercept(HookCall(application, emptyArray()) {
            attached = true
            "original"
        })
        assertEquals(listOf("impad_wecom_tinker_bootstrap"), removed)
        assertEquals(4, hooks.size)
    }

    @Test
    fun acceptsTheMainAndAllPackageScopedProcessesOnly() {
        val adapter = WeComAppAdapter()

        assertTrue(adapter.acceptsProcess("com.tencent.wework"))
        assertTrue(adapter.acceptsProcess("com.tencent.wework:wemeet"))
        assertTrue(adapter.acceptsProcess("com.tencent.wework:push"))
        assertTrue(adapter.acceptsProcess("com.tencent.wework:wxa_container0"))
        assertFalse(adapter.acceptsProcess("com.tencent.weworkx"))
        assertFalse(adapter.acceptsProcess(null))
    }
}
