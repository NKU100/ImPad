package io.github.nku100.impad.apps.wecom

import io.github.nku100.impad.runtime.HookCallback
import io.github.nku100.impad.runtime.HookRegistrar
import io.github.nku100.impad.runtime.HookRegistration
import io.github.nku100.impad.runtime.InstallOutcome
import java.lang.reflect.Method
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue
import org.junit.jupiter.api.Test

class WeComHookInstallerTest {
    @Test
    fun registersAllThreeReturnOverrides() {
        val registrar = RecordingRegistrar()

        val outcome = WeComHookInstaller(registrar).install(
            deviceTypeMethod(),
            applicationPadMethod(),
            servicePadMethod(),
        )

        assertIs<InstallOutcome.Installed>(outcome)
        assertEquals(listOf("impad_wecom_device_type", "impad_wecom_app_pad", "impad_wecom_service_pad"), registrar.attemptedIds)
        assertEquals(3, outcome.registrations.size)
    }

    @Test
    fun rollsBackEarlierHooksWhenARegistrationFails() {
        val registrar = RecordingRegistrar(failOnCall = 3)

        val outcome = WeComHookInstaller(registrar).install(
            deviceTypeMethod(),
            applicationPadMethod(),
            servicePadMethod(),
        )

        assertIs<InstallOutcome.Failed>(outcome)
        assertEquals(listOf("impad_wecom_app_pad", "impad_wecom_device_type"), registrar.removedIds)
        assertEquals(2, registrar.returnedRegistrations.size)
    }

    private fun deviceTypeMethod(): Method = javaClass.getDeclaredMethod("deviceTypeTarget")

    private fun applicationPadMethod(): Method = javaClass.getDeclaredMethod("applicationPadTarget")

    private fun servicePadMethod(): Method = javaClass.getDeclaredMethod("servicePadTarget")

    private fun deviceTypeTarget(): Int = 0

    private fun applicationPadTarget(): Boolean = false

    private fun servicePadTarget(): Boolean = false

    private class RecordingRegistrar(private val failOnCall: Int? = null) : HookRegistrar {
        val attemptedIds = mutableListOf<String>()
        val removedIds = mutableListOf<String>()
        val returnedRegistrations = mutableListOf<HookRegistration>()

        override fun hook(method: Method, id: String, callback: HookCallback): HookRegistration {
            attemptedIds += id
            if (attemptedIds.size == failOnCall) error("injected registration failure")
            return HookRegistration { removedIds += id }.also(returnedRegistrations::add)
        }
    }
}
