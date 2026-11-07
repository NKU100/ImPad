package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.compat.rules.PlanHook
import io.github.nku100.impad.rules.HookRoleContract

/** Field bindings belong to the inner method's declaring class and code source. */
fun validateHookConfiguration(hook: PlanHook, role: HookRoleContract) {
    require(hook.phase == role.phase)
    if (role.role != "inner") {
        require(hook.configuration == role.configuration) { "Hook configuration differs from its contract" }
        return
    }
    require(role.configuration == mapOf("policy" to "cache_true_after"))
    require(hook.configuration.keys == setOf("policy", "cacheField", "monitorField") &&
        hook.configuration["policy"] == "cache_true_after")
    val owner = hook.descriptor.substringBefore("->")
    for ((key, suffix) in listOf("cacheField" to ":Ljava/lang/Boolean;", "monitorField" to ":Ljava/lang/Object;")) {
        val descriptor = hook.configuration.getValue(key)
        if (key == "monitorField" && descriptor == owner) continue
        require(descriptor.startsWith("$owner->") && descriptor.endsWith(suffix))
        val name = descriptor.removePrefix("$owner->").removeSuffix(suffix)
        require(name.isNotEmpty() && name.none { it in ";:()/.[]" || it.isWhitespace() })
    }
    require(hook.shape.isStatic && hook.shape.returnType == "boolean" && hook.shape.parameters.size == 1)
}
