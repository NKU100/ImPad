package io.github.nku100.impad.apps.wecom

import io.github.nku100.impad.runtime.HookCall
import io.github.nku100.impad.runtime.HookCallback
import io.github.nku100.impad.runtime.HookRegistrar
import io.github.nku100.impad.runtime.HookRegistration
import io.github.nku100.impad.runtime.InstallOutcome
import java.lang.reflect.Method
import kotlin.test.assertEquals
import kotlin.test.assertIs
import org.junit.jupiter.api.Test

class WeComServiceDeviceTypeHookTest {
    @Test
    fun candidateServiceDeviceTypeHookPreservesPhoneAndTabletResults() {
        val registrar = RecordingRegistrar()

        val outcome = WeComHookInstaller(registrar).install(
            javaClass.getDeclaredMethod("getDeviceType"),
            javaClass.getDeclaredMethod("applicationPad"),
            javaClass.getDeclaredMethod("servicePad"),
        )

        assertIs<InstallOutcome.Installed>(outcome)
        val callback = registrar.callbacks.getValue("impad_wecom_device_type")
        var originalCalls = 0

        assertEquals(131073, callback.intercept(HookCall(null, emptyArray()) { originalCalls++; 131073 }))
        assertEquals(131076, callback.intercept(HookCall(null, emptyArray()) { originalCalls++; 131076 }))
        assertEquals(2, originalCalls)
    }

    private fun getDeviceType(): Int = 0

    private fun applicationPad(): Boolean = false

    private fun servicePad(): Boolean = false

    private class RecordingRegistrar : HookRegistrar {
        val callbacks = mutableMapOf<String, HookCallback>()

        override fun hook(method: Method, id: String, callback: HookCallback): HookRegistration {
            callbacks[id] = callback
            return HookRegistration {}
        }
    }
}
