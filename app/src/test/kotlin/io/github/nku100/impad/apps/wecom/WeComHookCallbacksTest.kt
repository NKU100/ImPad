package io.github.nku100.impad.apps.wecom

import io.github.nku100.impad.runtime.HookCall
import kotlin.test.assertEquals
import org.junit.jupiter.api.Test

class WeComHookCallbacksTest {
    @Test
    fun forcesThePadDeviceTypeWithoutCallingTheNativeResult() {
        var originalCalls = 0
        val result = WeComHookCallbacks().deviceType(
            HookCall(null, emptyArray()) { originalCalls++; 0 },
        )

        assertEquals(0x20004, result)
        assertEquals(0, originalCalls)
    }

    @Test
    fun reportsPadFromApplicationWithoutCallingTheOriginalMethod() {
        var originalCalls = 0
        val result = WeComHookCallbacks().applicationIsPad(
            HookCall(null, emptyArray()) { originalCalls++; false },
        )

        assertEquals(true, result)
        assertEquals(0, originalCalls)
    }

    @Test
    fun reportsPadFromServiceWithoutCallingTheOriginalMethod() {
        var originalCalls = 0
        val result = WeComHookCallbacks().serviceIsPad(
            HookCall(Any(), emptyArray()) { originalCalls++; false },
        )

        assertEquals(true, result)
        assertEquals(0, originalCalls)
    }
}
