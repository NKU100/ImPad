package io.github.nku100.impad.apps.qq

import io.github.nku100.impad.runtime.*
import java.lang.reflect.Method
import java.lang.reflect.Modifier

class QqHookInstaller(registrar: HookRegistrar) {
    private val installer = HookInstaller(registrar)

    fun install(deviceType: Method): InstallOutcome {
        require(Modifier.isStatic(deviceType.modifiers) && deviceType.returnType.isEnum &&
            deviceType.parameterTypes.contentEquals(arrayOf(android.content.Context::class.java))) {
            "QQ device type Getter must be static and return an enum"
        }
        val tablet = requireNotNull(deviceType.returnType.enumConstants?.singleOrNull {
            (it as? Enum<*>)?.name == "TABLET"
        }) { "QQ device enum must contain exactly one TABLET value" }
        return installer.install(listOf(
            HookSpec(deviceType, "impad_qq_device_type", HookCallback { tablet }),
        ))
    }
}
