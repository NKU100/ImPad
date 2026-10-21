package io.github.nku100.impad.apps.qq

import android.content.Context

import io.github.nku100.impad.compat.rules.MethodShape
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.ResolvedTarget
import io.github.nku100.impad.compat.rules.RuleRef
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

    @Test fun `missing tablet enum fails before any hook is registered`() {
        var installed = 0
        val plan = tabletPlan("qq_device_type_002")
        val target = plan.targets.getValue("device_type")
        val wrongDescriptor = "L${Fixture::class.java.name.replace('.', '/')};->wrong(" +
            "Landroid/content/Context;)L${WrongDevice::class.java.name.replace('.', '/')};"
        val wrongPlan = plan.copy(targets = mapOf(
            "device_type" to target.copy(
                descriptor = wrongDescriptor,
                shape = target.shape.copy(returnType = WrongDevice::class.java.name),
            ),
        ))
        val outcome = QqAppAdapter().installPlan(
            javaClass.classLoader!!,
            wrongPlan,
            HookRegistrar { _, _, _ -> installed++; HookRegistration {} },
            ModuleLog { _, _, _ -> },
        ) { true }
        assertIs<PlanInstallOutcome.Failed>(outcome)
        assertEquals(0, installed)
    }

    @Test fun `same tablet strategy accepts a different rule id with the complete target contract`() {
        val plan = tabletPlan("qq_device_type_002")
        val callbacks = mutableMapOf<String, HookCallback>()
        val outcome = QqAppAdapter().installPlan(
            javaClass.classLoader!!,
            plan,
            HookRegistrar { _, id, callback ->
                callbacks[id] = callback
                HookRegistration {}
            },
            ModuleLog { _, _, _ -> },
        ) { true }

        assertIs<PlanInstallOutcome.Installed>(outcome)
        assertEquals(Device.TABLET, callbacks.getValue("impad_qq_device_type")
            .intercept(HookCall(null, emptyArray()) { error("Original QQ getter must not run") }))
    }

    @Test fun `tablet strategy still rejects unknown strategy extra targets and wrong method shape`() {
        val installer = QqHookInstaller()
        val plan = tabletPlan("qq_device_type_002")

        assertFailsWith<IllegalArgumentException> {
            installer.preparePlan(javaClass.classLoader!!,
                plan.copy(rule = RuleRef(plan.rule.ruleId, "qq_unknown_001")))
        }
        assertFailsWith<IllegalArgumentException> {
            installer.preparePlan(javaClass.classLoader!!,
                plan.copy(targets = plan.targets + ("unexpected" to plan.targets.getValue("device_type"))))
        }
        assertFailsWith<IllegalArgumentException> {
            val target = plan.targets.getValue("device_type")
            installer.preparePlan(javaClass.classLoader!!,
                plan.copy(targets = mapOf("device_type" to target.copy(shape = target.shape.copy(isStatic = false))))
            )
        }
    }

    @Test fun `adapter accepts only the validated MSF process`() {
        val adapter = QqAppAdapter()
        assertTrue(adapter.acceptsProcess("com.tencent.mobileqq:MSF"))
        assertFalse(adapter.acceptsProcess("com.tencent.mobileqq"))
        assertFalse(adapter.acceptsProcess(null))
    }

    @Test fun `rule plan binds the explicit tablet strategy and checks freshness before registering`() {
        val adapter = QqAppAdapter()
        val plan = ResolvedPlan(
            RuleRef("qq_device_type_001", "qq_tablet_001"),
            mapOf("device_type" to ResolvedTarget(
                "L${Fixture::class.java.name.replace('.', '/')};->device(Landroid/content/Context;)L${Device::class.java.name.replace('.', '/')};",
                MethodShape(listOf(Context::class.java.name), Device::class.java.name, true),
            )),
        )
        val callbacks = mutableMapOf<String, HookCallback>()
        var registrations = 0
        val registrar = HookRegistrar { _, id, callback ->
            registrations++
            callbacks[id] = callback
            HookRegistration {}
        }
        val log = ModuleLog { _, _, _ -> }

        assertEquals(PlanInstallOutcome.StaleInputs,
            adapter.installPlan(javaClass.classLoader!!, plan, registrar, log) { false })
        assertEquals(0, registrations)
        assertIs<PlanInstallOutcome.Installed>(
            adapter.installPlan(javaClass.classLoader!!, plan, registrar, log) { true },
        )
        assertEquals(Device.TABLET, callbacks.getValue("impad_qq_device_type")
            .intercept(HookCall(null, emptyArray()) { error("Original QQ getter must not run") }))

        val wrongStrategy = plan.copy(rule = RuleRef("qq_device_type_001", "qq_service_001"))
        assertIs<PlanInstallOutcome.Failed>(
            adapter.installPlan(javaClass.classLoader!!, wrongStrategy, registrar, log) { true },
        )
        assertEquals(1, registrations)
    }

    private fun tabletPlan(ruleId: String) = ResolvedPlan(
        RuleRef(ruleId, "qq_tablet_001"),
        mapOf("device_type" to ResolvedTarget(
            "L${Fixture::class.java.name.replace('.', '/')};->device(Landroid/content/Context;)L${Device::class.java.name.replace('.', '/')};",
            MethodShape(listOf(Context::class.java.name), Device::class.java.name, true),
        )),
    )
}
