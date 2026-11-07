package io.github.nku100.impad.runtime

import android.content.Context
import java.lang.reflect.Method

class WeChatEarlyClassLoaderBridge(private val registrar: HookRegistrar) {
    fun install(defaultLoader: ClassLoader, onReady: (Context, ClassLoader) -> Unit,
        onLaterGeneration: () -> Unit, onFailure: (Throwable) -> Unit): HookRegistration {
        val type = defaultLoader.loadClass("com.tencent.tinker.loader.app.TinkerApplication")
        val method = type.getDeclaredMethod("createInlineFence", android.app.Application::class.java,
            Int::class.javaPrimitiveType, String::class.java, Boolean::class.javaPrimitiveType,
            Long::class.javaPrimitiveType, Long::class.javaPrimitiveType, android.content.Intent::class.java)
        require(method.returnType == android.os.Handler::class.java && !java.lang.reflect.Modifier.isStatic(method.modifiers))
        val field = type.getDeclaredField("mCurrentClassLoader").apply { isAccessible = true }
        val fenceField = type.getDeclaredField("mInlineFence").apply { isAccessible = true }
        require(field.type == ClassLoader::class.java && !java.lang.reflect.Modifier.isStatic(field.modifiers))
        require(fenceField.type == android.os.Handler::class.java && !java.lang.reflect.Modifier.isStatic(fenceField.modifiers))
        return installObserved(method, { receiver ->
            val context = receiver as Context
            require(fenceField.get(context) == null) { "Tinker delegate was already constructed" }
            val actual = field.get(context) as ClassLoader
            require(context.classLoader === actual) { "Tinker loader differs from Context loader" }
            actual
        }, { receiver, loader -> onReady(receiver as Context, loader) }, onLaterGeneration, onFailure)
    }

    internal fun installObserved(method: Method, readLoader: (Any) -> ClassLoader,
        onReady: (Any, ClassLoader) -> Unit, onLaterGeneration: () -> Unit,
        onFailure: (Throwable) -> Unit): HookRegistration {
        val lock = Any()
        var entered = false
        var completed = false
        return registrar.hook(method, "wechat_early_inline_fence") { call ->
            val action = synchronized(lock) {
                if (!entered) { entered = true; 1 } else if (completed) 2 else 0
            }
            try {
                if (action == 1) {
                    val receiver = checkNotNull(call.thisObject)
                    onReady(receiver, readLoader(receiver))
                } else if (action == 2) onLaterGeneration()
            } catch (error: Throwable) {
                if (error is VirtualMachineError || error is ThreadDeath) throw error
                try { onFailure(error) } catch (reportFailure: Throwable) {
                    if (reportFailure is VirtualMachineError || reportFailure is ThreadDeath) throw reportFailure
                }
            }
            try { call.proceed() } finally {
                if (action == 1) synchronized(lock) { completed = true }
            }
        }
    }
}
