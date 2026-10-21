package io.github.nku100.impad.apps.wecom

import io.github.nku100.impad.compat.rules.MethodShape
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.ResolvedTarget
import io.github.nku100.impad.compat.rules.RuleRef
import io.github.nku100.impad.runtime.HookCallback
import io.github.nku100.impad.runtime.HookRegistrar
import io.github.nku100.impad.runtime.HookRegistration
import io.github.nku100.impad.runtime.InstallOutcome
import java.lang.reflect.Method
import kotlin.test.assertEquals
import kotlin.test.assertIs
import org.junit.jupiter.api.Test

class WeComHookInstallerTest {
    @Test
    fun exposesOnlyExplicitRulePlanInstallation() {
        val installMethods = WeComHookInstaller::class.java.declaredMethods
            .filter { it.name == "install" }

        assertEquals(1, installMethods.size)
        assertEquals(ResolvedPlan::class.java, installMethods.single().parameterTypes.first())
    }

    @Test
    fun registersAllThreeReturnOverrides() {
        val registrar = RecordingRegistrar()

        val outcome = WeComHookInstaller(registrar).install(plan(), methods())

        assertIs<InstallOutcome.Installed>(outcome)
        assertEquals(
            listOf("impad_wecom_device_type", "impad_wecom_app_pad", "impad_wecom_service_pad"),
            registrar.attemptedIds,
        )
        assertEquals(3, outcome.registrations.size)
    }

    @Test
    fun rollsBackEarlierHooksWhenARegistrationFails() {
        val registrar = RecordingRegistrar(failOnCall = 3)

        val outcome = WeComHookInstaller(registrar).install(plan(), methods())

        assertIs<InstallOutcome.Failed>(outcome)
        assertEquals(listOf("impad_wecom_app_pad", "impad_wecom_device_type"), registrar.removedIds)
        assertEquals(2, registrar.returnedRegistrations.size)
    }

    private fun methods() = mapOf(
        "device_type" to javaClass.getDeclaredMethod("deviceTypeTarget"),
        "app_pad" to javaClass.getDeclaredMethod("applicationPadTarget"),
        "service_pad" to javaClass.getDeclaredMethod("servicePadTarget"),
    )

    private fun plan() = ResolvedPlan(
        RuleRef("wecom_local_001", "wecom_local_tablet_001"),
        mapOf(
            "device_type" to ResolvedTarget(
                "Lexample/Device;->deviceTypeTarget()I", MethodShape(emptyList(), "int", false),
            ),
            "app_pad" to ResolvedTarget(
                "Lexample/Device;->applicationPadTarget()Z", MethodShape(emptyList(), "boolean", false),
            ),
            "service_pad" to ResolvedTarget(
                "Lexample/Device;->servicePadTarget()Z", MethodShape(emptyList(), "boolean", false),
            ),
        ),
    )

    private fun deviceTypeTarget(): Int = 0

    private fun applicationPadTarget(): Boolean = false

    private fun servicePadTarget(): Boolean = false

    private class RecordingRegistrar(private val failOnCall: Int? = null) : HookRegistrar {
        val attemptedIds = mutableListOf<String>()
        val removedIds = mutableListOf<String>()
        val returnedRegistrations = mutableListOf<HookRegistration>()

        override fun hook(method: Method, id: String, callback: HookCallback): HookRegistration {
            attemptedIds += id
            if (attemptedIds.size == failOnCall) error("injected hook registration failure")
            return HookRegistration { removedIds += id }.also(returnedRegistrations::add)
        }
    }
}
