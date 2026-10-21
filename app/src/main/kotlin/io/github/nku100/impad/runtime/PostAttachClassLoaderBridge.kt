package io.github.nku100.impad.runtime

import android.content.Context
import java.lang.reflect.Method
import java.util.concurrent.atomic.AtomicBoolean

class PostAttachClassLoaderBridge(private val registrar: HookRegistrar) {
    fun install(
        method: Method,
        hookId: String,
        classLoaderAfterAttach: (HookCall) -> ClassLoader,
        onReady: (ClassLoader) -> Unit,
    ): HookRegistration = installReady(method, hookId, { call ->
        val loader = classLoaderAfterAttach(call)
        null to loader
    }) { _, loader -> onReady(loader) }

    fun installWithContext(
        method: Method,
        hookId: String,
        onReady: (Context, ClassLoader) -> Unit,
    ): HookRegistration = installReady(method, hookId, { call ->
        val context = checkNotNull(call.arguments.firstOrNull() as? Context) {
            "QFix attach did not provide its Context"
        }
        context to checkNotNull(context.classLoader) { "QFix Context did not provide a ClassLoader" }
    }) { context, loader -> onReady(checkNotNull(context), loader) }

    fun installWithContext(
        method: Method,
        hookId: String,
        classLoaderAfterAttach: (HookCall) -> ClassLoader,
        onReady: (Context, ClassLoader) -> Unit,
    ): HookRegistration = installReady(method, hookId, { call ->
        val context = checkNotNull(call.arguments.firstOrNull() as? Context) {
            "Class-loader attach did not provide its Context"
        }
        context to classLoaderAfterAttach(call)
    }) { context, loader -> onReady(checkNotNull(context), loader) }

    private fun installReady(
        method: Method,
        hookId: String,
        readyInfo: (HookCall) -> Pair<Context?, ClassLoader>,
        onReady: (Context?, ClassLoader) -> Unit,
    ): HookRegistration {
        // Claim on entry so a nested attach cannot install before its outer call returns.
        val claimed = AtomicBoolean(false)
        var registration: HookRegistration? = null
        registration = registrar.hook(method, hookId) { call ->
            val first = claimed.compareAndSet(false, true)
            try {
                val result = call.proceed()
                if (first) {
                    val (context, loader) = readyInfo(call)
                    onReady(context, loader)
                }
                result
            } finally {
                if (first) registration?.unhook()
            }
        }
        return registration
    }
}
