package io.github.nku100.impad.runtime

import android.content.Context
import io.github.nku100.impad.compat.rules.CompletePlan
import io.github.nku100.impad.compat.rules.HookPhase
import io.github.nku100.impad.compat.rules.MethodShape
import io.github.nku100.impad.compat.rules.PlanHook
import io.github.nku100.impad.compat.rules.TargetCodeSource
import io.github.nku100.impad.rules.SemanticAppCatalogs
import java.lang.reflect.Method
import java.lang.reflect.Modifier
import org.junit.jupiter.api.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertSame

class SemanticHookStrategiesTest {
    enum class Device { PHONE, TABLET }

    class Fixture {
        companion object {
            @JvmStatic fun qqDevice(@Suppress("UNUSED_PARAMETER") context: Context): Device = Device.PHONE
        }
        fun wechatTablet(): Boolean = false
        fun wechatLogin(@Suppress("UNUSED_PARAMETER") view: Any): String = "original"
        fun wecomDevice(): Int = 0
        fun wecomAppPad(): Boolean = false
        fun wecomServicePad(): Boolean = false
    }

    private val methods = mapOf(
        "device_type" to Fixture::class.java.getDeclaredMethod("qqDevice", Context::class.java),
        "tablet" to Fixture::class.java.getDeclaredMethod("wechatTablet"),
        "login" to Fixture::class.java.getDeclaredMethod("wechatLogin", Any::class.java),
        "app_pad" to Fixture::class.java.getDeclaredMethod("wecomAppPad"),
        "service_pad" to Fixture::class.java.getDeclaredMethod("wecomServicePad"),
    )

    @Test
    fun `prepares each complete catalog plan in contract role order`() {
        val strategies = SemanticHookStrategies()

        val qq = strategies.prepare(plan("qq", listOf("device_type")), mapOf("device_type" to methods.getValue("device_type")))
        assertEquals(listOf("impad_qq_device_type"), qq.map { it.id })
        assertEquals(Device.TABLET, qq.single().callback.intercept(HookCall(null, emptyArray()) { error("QQ original must not run") }))

        val wechat = strategies.prepare(plan("wechat", listOf("tablet", "login")), mapOf(
            "tablet" to methods.getValue("tablet"), "login" to methods.getValue("login"),
        ))
        assertEquals(listOf("wechatpad_tablet", "wechatpad_login"), wechat.map { it.id })
        var originalCalls = 0
        assertEquals(true, wechat[0].callback.intercept(HookCall(null, emptyArray()) { originalCalls++; false }))
        assertEquals("original", wechat[1].callback.intercept(HookCall(null, arrayOf(Any())) { originalCalls++; "original" }))
        assertEquals(2, originalCalls)

        val wecom = strategies.prepare(plan("wecom", listOf("device_type", "app_pad", "service_pad")), mapOf(
            "device_type" to Fixture::class.java.getDeclaredMethod("wecomDevice"),
            "app_pad" to methods.getValue("app_pad"), "service_pad" to methods.getValue("service_pad"),
        ))
        assertEquals(listOf("impad_wecom_device_type", "impad_wecom_app_pad", "impad_wecom_service_pad"), wecom.map { it.id })
        assertEquals(0x20004, wecom[0].callback.intercept(HookCall(null, emptyArray()) { error("WeCom original must not run") }))
        assertEquals(true, wecom[1].callback.intercept(HookCall(null, emptyArray()) { error("WeCom original must not run") }))
        assertEquals(true, wecom[2].callback.intercept(HookCall(null, emptyArray()) { error("WeCom original must not run") }))
    }

    @Test
    fun `rejects unknown contract strategy revision phase configuration and role set`() {
        val strategies = SemanticHookStrategies()
        val valid = plan("qq", listOf("device_type"))
        val methodMap = mapOf("device_type" to methods.getValue("device_type"))
        val hook = valid.hooks.single()

        listOf(
            rebuild(valid, contractId = "unknown"),
            rebuild(valid, contractRevision = valid.contractRevision + 1),
            rebuild(valid, strategyId = "unknown"),
            rebuild(valid, strategyRevision = valid.strategyRevision + 1),
            rebuild(valid, hooks = listOf(hook(role = hook.role, phase = HookPhase.BEFORE, configuration = hook.configuration))),
            rebuild(valid, hooks = listOf(hook(role = hook.role, phase = hook.phase, configuration = mapOf("enum" to "PHONE")))),
        ).forEach { mutated -> assertFailsWith<IllegalArgumentException> { strategies.prepare(mutated, methodMap) } }

        assertFailsWith<IllegalArgumentException> { strategies.prepare(valid, emptyMap()) }
        assertFailsWith<IllegalArgumentException> {
            strategies.prepare(valid, methodMap + ("extra" to methods.getValue("tablet")))
        }
        assertSame(methods.getValue("device_type"), strategies.prepare(valid, methodMap).single().method)
    }

    @Test
    fun `rejects reversed WeChat and WeCom plan roles`() {
        val strategies = SemanticHookStrategies()
        val wechat = plan("wechat", listOf("tablet", "login"))
        val wechatMethods = mapOf("tablet" to methods.getValue("tablet"), "login" to methods.getValue("login"))
        assertFailsWith<IllegalArgumentException> {
            strategies.prepare(rebuild(wechat, hooks = wechat.hooks.reversed()), wechatMethods)
        }

        val wecom = plan("wecom", listOf("device_type", "app_pad", "service_pad"))
        val wecomMethods = wecom.hooks.associate { hook -> hook.role to methodFor("wecom", hook.role) }
        assertFailsWith<IllegalArgumentException> {
            strategies.prepare(rebuild(wecom, hooks = wecom.hooks.reversed()), wecomMethods)
        }
    }

    private fun plan(appId: String, roles: List<String>): CompletePlan {
        val catalog = SemanticAppCatalogs.forApp(appId)
        val strategy = catalog.strategies.single()
        val source = TargetCodeSource("loader", 0, "a".repeat(64))
        val hooks = strategy.roles.map { role ->
            val method = methodFor(appId, role.role)
            PlanHook(role.role, method.descriptor(), method.shape(), source, role.phase, role.configuration)
        }
        require(hooks.map { it.role } == roles)
        return CompletePlan("b".repeat(64), catalog.contractId, catalog.contractRevision,
            strategy.id, strategy.revision, hooks)
    }

    private fun methodFor(appId: String, role: String): Method = when (appId to role) {
        "qq" to "device_type" -> methods.getValue("device_type")
        "wechat" to "tablet" -> methods.getValue("tablet")
        "wechat" to "login" -> methods.getValue("login")
        "wecom" to "device_type" -> Fixture::class.java.getDeclaredMethod("wecomDevice")
        "wecom" to "app_pad" -> methods.getValue("app_pad")
        "wecom" to "service_pad" -> methods.getValue("service_pad")
        else -> error("No fixture method for $appId/$role")
    }

    private fun Method.shape() = MethodShape(
        parameterTypes.map(Class<*>::getName), returnType.name, Modifier.isStatic(modifiers),
    )

    private fun Method.descriptor(): String =
        "L${declaringClass.name.replace('.', '/')};->$name(${parameterTypes.joinToString("") { it.descriptor() }})${returnType.descriptor()}"

    private fun Class<*>.descriptor(): String = when {
        isArray -> name.replace('.', '/')
        this == java.lang.Void.TYPE -> "V"
        this == java.lang.Boolean.TYPE -> "Z"
        this == java.lang.Byte.TYPE -> "B"
        this == java.lang.Character.TYPE -> "C"
        this == java.lang.Short.TYPE -> "S"
        this == java.lang.Integer.TYPE -> "I"
        this == java.lang.Long.TYPE -> "J"
        this == java.lang.Float.TYPE -> "F"
        this == java.lang.Double.TYPE -> "D"
        else -> "L${name.replace('.', '/')};"
    }

    private fun rebuild(
        plan: CompletePlan,
        contractId: String = plan.contractId,
        contractRevision: Int = plan.contractRevision,
        strategyId: String = plan.strategyId,
        strategyRevision: Int = plan.strategyRevision,
        hooks: List<PlanHook> = plan.hooks,
    ) = CompletePlan(plan.snapshotSha256, contractId, contractRevision, strategyId, strategyRevision, hooks)

    private fun hook(role: String, phase: HookPhase, configuration: Map<String, String>) = PlanHook(
        role, "Lfixture/Target;->$role()Ljava/lang/Object;", MethodShape(emptyList(), "java.lang.Object", false),
        TargetCodeSource("loader", 0, "a".repeat(64)), phase, configuration,
    )
}
