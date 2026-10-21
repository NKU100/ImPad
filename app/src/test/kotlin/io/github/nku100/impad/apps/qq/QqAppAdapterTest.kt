package io.github.nku100.impad.apps.qq

import io.github.nku100.impad.runtime.*
import org.junit.jupiter.api.Test
import kotlin.test.*

class QqAppAdapterTest {
    class HostContext(private val loader: ClassLoader) : android.content.ContextWrapper(null) {
        override fun getClassLoader(): ClassLoader = loader
    }

    @Test
    fun ruleBridgeDeliversTheAttachContextAndItsLoaderOnlyAfterSuccess() {
        var callback: HookCallback? = null
        var attached = false
        var readyContext: android.content.Context? = null
        var readyLoader: ClassLoader? = null
        val context = HostContext(javaClass.classLoader!!)
        QqAppAdapter().prepareClassLoader(
            javaClass.classLoader!!,
            HookRegistrar { _, _, hook -> callback = hook; HookRegistration {} },
        ) { observedContext, observedLoader ->
            assertTrue(attached)
            readyContext = observedContext
            readyLoader = observedLoader
        }

        assertNull(readyContext)
        assertNotNull(callback).intercept(HookCall(null, arrayOf(context)) {
            attached = true
            "attached"
        })

        assertSame(context, readyContext)
        assertSame(context.classLoader, readyLoader)
    }
}
