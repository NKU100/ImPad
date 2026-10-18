package io.github.nku100.impad.runtime

import android.content.Context

class TinkerClassLoaderBridge(
    private val registrar: HookRegistrar,
    private val postAttachClassLoader: (Any) -> ClassLoader = { (it as Context).classLoader },
) {
    fun install(classLoader: ClassLoader, hookId: String = BOOTSTRAP_HOOK_ID,
                onReady: (ClassLoader) -> Unit): HookRegistration {
        val applicationClass = classLoader.loadClass(TINKER_APPLICATION_CLASS)
        val method = applicationClass.getDeclaredMethod(
            BASE_CONTEXT_ATTACHED_METHOD,
            Context::class.java,
            Long::class.javaPrimitiveType,
            Long::class.javaPrimitiveType,
        )
        return PostAttachClassLoaderBridge(registrar).install(method, hookId, { call ->
            val application = checkNotNull(call.thisObject) { "TinkerApplication receiver was null" }
            postAttachClassLoader(application)
        }, onReady)
    }

    private companion object {
        const val TINKER_APPLICATION_CLASS = "com.tencent.tinker.loader.app.TinkerApplication"
        const val BASE_CONTEXT_ATTACHED_METHOD = "onBaseContextAttached"
        const val BOOTSTRAP_HOOK_ID = "wechatpad_tinker_bootstrap"
    }
}
