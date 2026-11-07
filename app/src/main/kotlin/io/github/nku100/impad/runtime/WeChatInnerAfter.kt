package io.github.nku100.impad.runtime

import java.lang.reflect.Field
import java.lang.reflect.Method
import java.lang.reflect.Modifier

object WeChatInnerAfter {
    fun bind(decision: Method, cache: Field, monitor: Field?): HookCallback {
        val owner = decision.declaringClass
        require(Modifier.isStatic(decision.modifiers) && decision.returnType == Boolean::class.javaPrimitiveType &&
            decision.parameterTypes.size == 1 && !decision.parameterTypes.single().isPrimitive)
        require(cache.declaringClass == owner && Modifier.isStatic(cache.modifiers) &&
            !Modifier.isFinal(cache.modifiers) && cache.type == java.lang.Boolean::class.java)
        require(monitor == null || (monitor.declaringClass == owner && Modifier.isStatic(monitor.modifiers) && monitor.type == Any::class.java))
        cache.isAccessible = true
        monitor?.isAccessible = true
        // Read the monitor after the original invocation, preserving its class initialization and failure semantics.
        return HookCallback { call ->
            call.proceed()
            synchronized(if (monitor == null) owner else checkNotNull(monitor.get(null))) {
                cache.set(null, java.lang.Boolean.TRUE)
                check(cache.get(null) == java.lang.Boolean.TRUE) { "Tablet cache correction did not retain true" }
            }
            true
        }
    }
}
