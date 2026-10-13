package io.github.nku100.impad.apps.qq

import android.content.Context

import io.github.nku100.impad.runtime.*
import org.junit.jupiter.api.Test
import kotlin.test.*

class QqHookInstallerTest {
    enum class Device { PHONE, TABLET }
    enum class WrongDevice { PHONE }
    class Fixture {
        companion object {
            @JvmStatic fun device(context: Context?): Device = Device.PHONE
            @JvmStatic fun wrong(context: Context?): WrongDevice = WrongDevice.PHONE
        }
    }

    @Test fun `tablet hook returns enum without an appid observation hook`() {
        val calls = mutableMapOf<String, HookCallback>()
        val installer = QqHookInstaller(HookRegistrar { _, id, callback ->
            calls[id] = callback
            HookRegistration { calls.remove(id) }
        })
        assertIs<InstallOutcome.Installed>(installer.install(
            Fixture::class.java.getDeclaredMethod("device", Context::class.java)))
        assertEquals(Device.TABLET, calls.getValue("impad_qq_device_type").intercept(
            HookCall(null, emptyArray()) { error("Original device getter must not run") }))
        assertEquals(setOf("impad_qq_device_type"), calls.keys)
    }

    @Test fun `missing tablet enum fails before any hook is registered`() {
        var installed = 0
        val installer = QqHookInstaller(HookRegistrar { _, _, _ -> installed++; HookRegistration {} })
        assertFailsWith<IllegalArgumentException> { installer.install(
            Fixture::class.java.getDeclaredMethod("wrong", Context::class.java)) }
        assertEquals(0, installed)
    }

    @Test fun `adapter accepts only the validated MSF process`() {
        val adapter = QqAppAdapter()
        assertTrue(adapter.acceptsProcess("com.tencent.mobileqq:MSF"))
        assertFalse(adapter.acceptsProcess("com.tencent.mobileqq"))
        assertFalse(adapter.acceptsProcess(null))
    }
}
