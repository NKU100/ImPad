package io.github.nku100.impad.runtime

import kotlin.test.*

class WeChatInnerAfterTest {
    @Test fun preservesOriginalWidthAndCorrectsCacheAndReturn() {
        Host.cache = null; Host.width = 0
        val callback = WeChatInnerAfter.bind(Host::class.java.getDeclaredMethod("decision", String::class.java),
            Host::class.java.getDeclaredField("cache"), Host::class.java.getDeclaredField("monitor"))
        var executed = false
        assertEquals(true, callback.intercept(HookCall(null, arrayOf("width")) {
            executed = true; Host.width = 411; Host.cache = false; false
        }))
        assertTrue(executed); assertEquals(411, Host.width); assertEquals(true, Host.cache)
    }
    @Test fun propagatesOriginalExceptionWithoutWritingCache() {
        Host.cache = null
        val callback = WeChatInnerAfter.bind(Host::class.java.getDeclaredMethod("decision", String::class.java),
            Host::class.java.getDeclaredField("cache"), Host::class.java.getDeclaredField("monitor"))
        val failure = IllegalStateException("original")
        assertSame(failure, assertFailsWith<IllegalStateException> {
            callback.intercept(HookCall(null, emptyArray()) { throw failure })
        })
        assertNull(Host.cache)
    }
    @Test fun rejectsUnrelatedOrFinalFields() {
        val method = Host::class.java.getDeclaredMethod("decision", String::class.java)
        assertFailsWith<IllegalArgumentException> { WeChatInnerAfter.bind(method,
            Other::class.java.getDeclaredField("cache"), Host::class.java.getDeclaredField("monitor")) }
        assertFailsWith<IllegalArgumentException> { WeChatInnerAfter.bind(method,
            Host::class.java.getDeclaredField("constant"), Host::class.java.getDeclaredField("monitor")) }
    }
    class Host { companion object {
        @JvmField var cache: Boolean? = null
        @JvmField var width: Int = 0
        @JvmField val monitor: Any = Any()
        @JvmField val constant: Boolean? = true
        @JvmStatic fun decision(value: String): Boolean = value.isNotEmpty()
    } }
    class Other { companion object { @JvmField var cache: Boolean? = null } }
}
