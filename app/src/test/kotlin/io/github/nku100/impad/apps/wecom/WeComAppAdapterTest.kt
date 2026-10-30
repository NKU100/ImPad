package io.github.nku100.impad.apps.wecom

import android.content.Context
import android.content.ContextWrapper
import io.github.nku100.impad.compat.rules.MethodShape
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.ResolvedTarget
import io.github.nku100.impad.compat.rules.RuleRef
import io.github.nku100.impad.runtime.HookCall
import io.github.nku100.impad.runtime.HookCallback
import io.github.nku100.impad.runtime.HookRegistrar
import io.github.nku100.impad.runtime.HookRegistration
import io.github.nku100.impad.runtime.ModuleLog
import io.github.nku100.impad.runtime.PlanInstallOutcome
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertSame
import kotlin.test.assertTrue
import org.junit.jupiter.api.Test

class WeComAppAdapterTest {
    class RuleTargets {
        fun getDeviceType(): Int = 0
        fun appPad(): Boolean = false
        fun servicePad(): Boolean = false
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

    @Test
    fun waitsForTinkerThenInstallsTheExplicitLocalStrategyAgainstTheReadyLoader() {
        val adapter = WeComAppAdapter()
        val loader = javaClass.classLoader!!
        val application = com.tencent.tinker.loader.app.TinkerApplication(loader)
        val attachContextLoader = object : ClassLoader(loader) {}
        val attachContext = object : ContextWrapper(null) {
            override fun getClassLoader(): ClassLoader = attachContextLoader
        }
        val registrar = RecordingRegistrar()
        var attached = false
        var readyContext: Context? = null
        var readyLoader: ClassLoader? = null
        var installResult: PlanInstallOutcome? = null
        var laterReadyCalls = 0

        adapter.prepareClassLoader(loader, registrar, onLaterReady = { laterReadyCalls++ }) { context, contextLoader ->
            assertTrue(attached)
            readyContext = context
            readyLoader = contextLoader
            installResult = adapter.installPlan(contextLoader, rulePlan(), registrar,
                ModuleLog { _, _, _ -> }) { true }
        }

        assertEquals(setOf("impad_wecom_tinker_bootstrap"), registrar.callbacks.keys)
        val originalResult = registrar.callbacks.getValue("impad_wecom_tinker_bootstrap").intercept(
            HookCall(application, arrayOf(attachContext, 0L, 0L)) { attached = true; "attached" },
        )

        assertEquals("attached", originalResult)
        assertSame(attachContext, readyContext)
        assertSame(loader, readyLoader)
        assertEquals("tinker_ready", adapter.classLoaderReadyEvent)
        assertIs<PlanInstallOutcome.Installed>(installResult)
        assertEquals(
            listOf("impad_wecom_tinker_bootstrap", "impad_wecom_device_type", "impad_wecom_app_pad", "impad_wecom_service_pad"),
            registrar.attemptedIds,
        )
        registrar.callbacks.getValue("impad_wecom_tinker_bootstrap").intercept(
            HookCall(application, arrayOf(attachContext, 0L, 0L)) { "later" },
        )
        assertEquals(1, laterReadyCalls)
        var originalCalls = 0
        assertEquals(0x20004, registrar.callbacks.getValue("impad_wecom_device_type")
            .intercept(HookCall(null, emptyArray()) { originalCalls++; 1 }))
        assertEquals(0, originalCalls)
    }

    @Test
    fun rollsBackTheEntireLocalRuleWhenTheThirdHookRegistrationFails() {
        val registrar = RecordingRegistrar(failOnCall = 3)
        val result = WeComAppAdapter().installPlan(
            javaClass.classLoader!!,
            rulePlan(),
            registrar,
            ModuleLog { _, _, _ -> },
        ) { true }

        assertIs<PlanInstallOutcome.Failed>(result)
        assertEquals(
            listOf("impad_wecom_app_pad", "impad_wecom_device_type"),
            registrar.removedIds,
        )
    }

    private fun rulePlan(): ResolvedPlan {
        val targetClass = RuleTargets::class.java.name.replace('.', '/')
        return ResolvedPlan(
            rule = RuleRef("wecom_local_001", "wecom_local_tablet_001"),
            targets = mapOf(
                "device_type" to ResolvedTarget(
                    "L$targetClass;->getDeviceType()I",
                    MethodShape(emptyList(), "int", false),
                ),
                "app_pad" to ResolvedTarget(
                    "L$targetClass;->appPad()Z",
                    MethodShape(emptyList(), "boolean", false),
                ),
                "service_pad" to ResolvedTarget(
                    "L$targetClass;->servicePad()Z",
                    MethodShape(emptyList(), "boolean", false),
                ),
            ),
        )
    }

    private class RecordingRegistrar(private val failOnCall: Int? = null) : HookRegistrar {
        val callbacks = mutableMapOf<String, HookCallback>()
        val attemptedIds = mutableListOf<String>()
        val removedIds = mutableListOf<String>()

        override fun hook(method: java.lang.reflect.Method, id: String, callback: HookCallback): HookRegistration {
            attemptedIds += id
            if (attemptedIds.size == failOnCall) error("injected hook registration failure")
            callbacks[id] = callback
            return HookRegistration { removedIds += id }
        }
    }
}
