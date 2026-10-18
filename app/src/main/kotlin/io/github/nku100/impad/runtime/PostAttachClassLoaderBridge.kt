package io.github.nku100.impad.runtime

import java.lang.reflect.Method
import java.util.concurrent.atomic.AtomicBoolean

class PostAttachClassLoaderBridge(private val registrar: HookRegistrar) {
    fun install(
        method: Method,
        hookId: String,
        classLoaderAfterAttach: (HookCall) -> ClassLoader,
        onReady: (ClassLoader) -> Unit,
    ): HookRegistration {
        // Claim on entry so a nested attach cannot install before its outer call returns.
        val claimed = AtomicBoolean(false)
        var registration: HookRegistration? = null
        registration = registrar.hook(method, hookId) { call ->
            val first = claimed.compareAndSet(false, true)
            try {
                val result = call.proceed()
                if (first) onReady(classLoaderAfterAttach(call))
                result
            } finally {
                if (first) registration?.unhook()
            }
        }
        return registration
    }
}
