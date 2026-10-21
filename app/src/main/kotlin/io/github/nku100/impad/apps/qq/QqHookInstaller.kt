package io.github.nku100.impad.apps.qq

import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.runtime.HookCallback
import io.github.nku100.impad.runtime.*
import java.lang.reflect.Method
import java.lang.reflect.Modifier

class QqHookInstaller {
    fun preparePlan(classLoader: ClassLoader, plan: ResolvedPlan): List<HookSpec> {
        require(plan.rule.strategyId == "qq_tablet_001") {
            "QQ rule plan did not select the registered tablet strategy"
        }
        require(plan.targets.keys == setOf("device_type")) { "QQ rule plan had an unexpected target set" }
        val target = plan.targets.getValue("device_type")
        val deviceType = TargetMethodResolver.resolve(classLoader, target.descriptor, target.shape)
        return listOf(prepareDeviceType(deviceType))
    }

    private fun prepareDeviceType(deviceType: Method): HookSpec {
        require(Modifier.isStatic(deviceType.modifiers) && deviceType.returnType.isEnum &&
            deviceType.parameterTypes.contentEquals(arrayOf(android.content.Context::class.java))) {
            "QQ device type Getter must be static, accept Context, and return an enum"
        }
        val tablet = requireNotNull(deviceType.returnType.enumConstants?.singleOrNull {
            (it as? Enum<*>)?.name == "TABLET"
        }) { "QQ device enum must contain exactly one TABLET value" }
        return HookSpec(deviceType, "impad_qq_device_type", HookCallback { tablet })
    }
}
