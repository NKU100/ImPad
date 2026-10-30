package io.github.nku100.impad.apps.wechat

import android.content.Context
import android.content.ContextWrapper
import com.tencent.tinker.loader.app.TinkerApplication
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
import java.lang.reflect.Method
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertSame
import kotlin.test.assertTrue

class WeChatAppAdapterTest {
    class RuleTargets {
        fun tablet(): Boolean = false
        fun login(): Unit = Unit
    }

    @Test
    fun acceptsOnlyTheMainWeChatProcessAndUsesTinkerReady() {
        val adapter = WeChatAppAdapter()
        assertTrue(adapter.acceptsProcess("com.tencent.mm"))
        assertFalse(adapter.acceptsProcess("com.tencent.mm:tools"))
        assertFalse(adapter.acceptsProcess("com.tencent.mmx"))
        assertFalse(adapter.acceptsProcess(null))
        assertEquals("tinker_ready", adapter.classLoaderReadyEvent)
    }

    @Test
    fun waitsUntilTinkerAttachReturnsThenInstallsBothExplicitRuleTargets() {
        val adapter = WeChatAppAdapter()
        val loader = javaClass.classLoader!!
        val application = TinkerApplication(loader)
        val contextLoader = object : ClassLoader(loader) {}
        val context = object : ContextWrapper(null) {
            override fun getClassLoader(): ClassLoader = contextLoader
        }
        val registrar = RecordingRegistrar()
        var attached = false
        var readyContext: Context? = null
        var readyLoader: ClassLoader? = null
        var installResult: PlanInstallOutcome? = null
        var laterReadyCalls = 0

        adapter.prepareClassLoader(loader, registrar, onLaterReady = { laterReadyCalls++ }) { readyContextValue, readyLoaderValue ->
            assertTrue(attached)
            readyContext = readyContextValue
            readyLoader = readyLoaderValue
            installResult = adapter.installPlan(readyLoaderValue, rulePlan(), registrar, ModuleLog { _, _, _ -> }) { true }
        }
        val result = registrar.callbacks.getValue("impad_wechat_tinker_bootstrap").intercept(
            HookCall(application, arrayOf(context, 0L, 0L)) { attached = true; "attached" },
        )

        assertEquals("attached", result)
        assertSame(context, readyContext)
        assertSame(loader, readyLoader)
        assertIs<PlanInstallOutcome.Installed>(installResult)
        assertEquals(listOf("impad_wechat_tinker_bootstrap", "wechatpad_tablet", "wechatpad_login"),
                     registrar.attemptedIds)
        registrar.callbacks.getValue("impad_wechat_tinker_bootstrap").intercept(
            HookCall(application, arrayOf(context, 0L, 0L)) { "later" },
        )
        assertEquals(1, laterReadyCalls)
    }

    @Test
    fun staleInputsAndAnyMethodShapeMismatchRegisterNoHooks() {
        val registrar = RecordingRegistrar()
        val adapter = WeChatAppAdapter()
        val stale = adapter.installPlan(javaClass.classLoader!!, rulePlan(), registrar,
            ModuleLog { _, _, _ -> }) { false }
        assertIs<PlanInstallOutcome.StaleInputs>(stale)
        assertTrue(registrar.attemptedIds.isEmpty())

        val wrongShape = rulePlan().copy(targets = rulePlan().targets + (
            "login" to ResolvedTarget(loginDescriptor(), MethodShape(emptyList(), "void", true))))
        val failed = adapter.installPlan(javaClass.classLoader!!, wrongShape, registrar,
            ModuleLog { _, _, _ -> }) { true }
        assertIs<PlanInstallOutcome.Failed>(failed)
        assertTrue(registrar.attemptedIds.isEmpty())
    }

    @Test
    fun rejectsAnUnregisteredRuleOrIncompleteTargetSetBeforeResolvingMethods() {
        val registrar = RecordingRegistrar()
        val adapter = WeChatAppAdapter()
        val wrongRule = rulePlan().copy(rule = RuleRef("other", "other"))
        assertIs<PlanInstallOutcome.Failed>(adapter.installPlan(javaClass.classLoader!!, wrongRule,
            registrar, ModuleLog { _, _, _ -> }) { true })
        val missing = rulePlan().copy(targets = mapOf("tablet" to rulePlan().targets.getValue("tablet")))
        assertIs<PlanInstallOutcome.Failed>(adapter.installPlan(javaClass.classLoader!!, missing,
            registrar, ModuleLog { _, _, _ -> }) { true })
        assertTrue(registrar.attemptedIds.isEmpty())
    }

    @Test
    fun rollsBackTabletHookIfLoginRegistrationFails() {
        val registrar = RecordingRegistrar(failOnCall = 2)
        val outcome = WeChatAppAdapter().installPlan(javaClass.classLoader!!, rulePlan(), registrar,
            ModuleLog { _, _, _ -> }) { true }

        assertIs<PlanInstallOutcome.Failed>(outcome)
        assertEquals(listOf("wechatpad_tablet"), registrar.removedIds)
        assertEquals(listOf("wechatpad_tablet", "wechatpad_login"), registrar.attemptedIds)
    }

    private fun rulePlan(): ResolvedPlan {
        val owner = RuleTargets::class.java.name.replace('.', '/')
        return ResolvedPlan(
            rule = RuleRef("wechat_tablet_login_001", "wechat_tablet_login_behavior_001"),
            targets = mapOf(
                "tablet" to ResolvedTarget("L$owner;->tablet()Z", MethodShape(emptyList(), "boolean", false)),
                "login" to ResolvedTarget(loginDescriptor(), MethodShape(emptyList(), "void", false)),
            ),
        )
    }

    private fun loginDescriptor(): String =
        "L${RuleTargets::class.java.name.replace('.', '/')};->login()V"

    private class RecordingRegistrar(private val failOnCall: Int? = null) : HookRegistrar {
        val callbacks = mutableMapOf<String, HookCallback>()
        val attemptedIds = mutableListOf<String>()
        val removedIds = mutableListOf<String>()

        override fun hook(method: Method, id: String, callback: HookCallback): HookRegistration {
            attemptedIds += id
            if (attemptedIds.size == failOnCall) error("injected WeChat hook registration failure")
            callbacks[id] = callback
            return HookRegistration { removedIds += id }
        }
    }
}
