package io.github.nku100.impad.apps.wecom

import io.github.nku100.impad.runtime.HookCall

class WeComHookCallbacks {
    fun deviceType(@Suppress("UNUSED_PARAMETER") call: HookCall): Any = PAD_DEVICE_TYPE

    fun serviceDeviceType(call: HookCall): Any? = call.proceed()

    fun applicationIsPad(@Suppress("UNUSED_PARAMETER") call: HookCall): Any = true

    fun serviceIsPad(@Suppress("UNUSED_PARAMETER") call: HookCall): Any = true

    private companion object {
        const val PAD_DEVICE_TYPE = 0x20004
    }
}
