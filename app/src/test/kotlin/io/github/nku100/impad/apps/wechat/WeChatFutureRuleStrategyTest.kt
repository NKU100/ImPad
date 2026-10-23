package io.github.nku100.impad.apps.wechat

import io.github.nku100.impad.compat.rules.MethodShape
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.ResolvedTarget
import io.github.nku100.impad.compat.rules.RuleRef
import io.github.nku100.impad.runtime.HookCallback
import io.github.nku100.impad.runtime.HookRegistrar
import io.github.nku100.impad.runtime.HookRegistration
import io.github.nku100.impad.runtime.ModuleLog
import io.github.nku100.impad.runtime.PlanInstallOutcome
import java.lang.reflect.Method
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

class WeChatFutureRuleStrategyTest {
    class RuleTargets {
        fun tablet(): Boolean = false
        fun login(): Unit = Unit
    }

    @Test
    fun installsBothTargetsForTheNewRuleAndItsExplicitStrategy() {
        val registrar = RecordingRegistrar()
        val result = WeChatAppAdapter().installPlan(
            javaClass.classLoader!!,
            futurePlan(),
            registrar,
            ModuleLog { _, _, _ -> },
        ) { true }

        assertIs<PlanInstallOutcome.Installed>(result)
        assertEquals(listOf("wechatpad_tablet", "wechatpad_login"), registrar.attemptedIds)
        assertEquals(2, result.registrations.size)
    }

    @Test
    fun rejectsTheNewRuleWhenPairedWithAnotherStrategy() {
        val registrar = RecordingRegistrar()
        val plan = futurePlan().copy(
            rule = RuleRef("wechat_tablet_login_002", "wechat_tablet_login_behavior_001"),
        )

        val result = WeChatAppAdapter().installPlan(
            javaClass.classLoader!!,
            plan,
            registrar,
            ModuleLog { _, _, _ -> },
        ) { true }

        assertIs<PlanInstallOutcome.Failed>(result)
        assertTrue(registrar.attemptedIds.isEmpty())
    }

    private fun futurePlan(): ResolvedPlan {
        val owner = RuleTargets::class.java.name.replace('.', '/')
        return ResolvedPlan(
            rule = RuleRef("wechat_tablet_login_002", "wechat_tablet_login_behavior_002"),
            targets = mapOf(
                "tablet" to ResolvedTarget("L$owner;->tablet()Z", MethodShape(emptyList(), "boolean", false)),
                "login" to ResolvedTarget("L$owner;->login()V", MethodShape(emptyList(), "void", false)),
            ),
        )
    }

    private class RecordingRegistrar : HookRegistrar {
        val attemptedIds = mutableListOf<String>()

        override fun hook(method: Method, id: String, callback: HookCallback): HookRegistration {
            attemptedIds += id
            return HookRegistration { }
        }
    }
}
