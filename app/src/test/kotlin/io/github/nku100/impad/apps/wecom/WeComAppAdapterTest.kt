package io.github.nku100.impad.apps.wecom

import kotlin.test.assertFalse
import kotlin.test.assertTrue
import org.junit.jupiter.api.Test

class WeComAppAdapterTest {
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
}
