package io.github.nku100.impad.apps.wecom

import io.github.nku100.impad.runtime.HookCallback
import io.github.nku100.impad.runtime.HookInstaller
import io.github.nku100.impad.runtime.HookRegistrar
import io.github.nku100.impad.runtime.HookSpec
import io.github.nku100.impad.runtime.InstallOutcome
import java.lang.reflect.Method

class WeComHookInstaller(
    registrar: HookRegistrar,
    private val callbacks: WeComHookCallbacks = WeComHookCallbacks(),
) {
    private val installer = HookInstaller(registrar)

    fun install(
        deviceTypeMethod: Method,
        applicationPadMethod: Method,
        servicePadMethod: Method,
    ): InstallOutcome {
        val deviceTypeCallback = if (
            deviceTypeMethod.name == SERVICE_DEVICE_TYPE_METHOD &&
            deviceTypeMethod.parameterTypes.isEmpty() &&
            deviceTypeMethod.returnType == Int::class.javaPrimitiveType
        ) {
            HookCallback(callbacks::serviceDeviceType)
        } else {
            HookCallback(callbacks::deviceType)
        }
        return installer.install(
            listOf(
                HookSpec(deviceTypeMethod, DEVICE_TYPE_HOOK_ID, deviceTypeCallback),
                HookSpec(applicationPadMethod, APPLICATION_PAD_HOOK_ID, HookCallback(callbacks::applicationIsPad)),
                HookSpec(servicePadMethod, SERVICE_PAD_HOOK_ID, HookCallback(callbacks::serviceIsPad)),
            ),
        )
    }

    private companion object {
        const val SERVICE_DEVICE_TYPE_METHOD = "getDeviceType"
        const val DEVICE_TYPE_HOOK_ID = "impad_wecom_device_type"
        const val APPLICATION_PAD_HOOK_ID = "impad_wecom_app_pad"
        const val SERVICE_PAD_HOOK_ID = "impad_wecom_service_pad"
    }
}
