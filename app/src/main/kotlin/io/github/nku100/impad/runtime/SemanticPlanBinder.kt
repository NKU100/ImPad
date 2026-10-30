package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.code.CodeSnapshot
import io.github.nku100.impad.compat.rules.CompletePlan
import io.github.nku100.impad.rules.SemanticRuleCatalog
import java.lang.reflect.Method
import java.util.Collections

object SemanticPlanBinder {
    fun bind(plan: CompletePlan, snapshot: CodeSnapshot, layout: StartupLoaderLayout, catalog: SemanticRuleCatalog): Map<String, Method> {
        CachedSemanticPlan.validateContract(plan, snapshot, catalog)
        require(snapshot.readyLoaderId == layout.readyLoaderId && snapshot.loaders.size == layout.loaders.size)
        val models = snapshot.loaders.associateBy { it.id }
        require(layout.bindings.keys == models.keys)
        layout.loaders.forEach { loader ->
            val model = requireNotNull(models[loader.id])
            require(model.parentId == loader.parentId && model.delegation == loader.delegation) { "Loader topology differs from the plan snapshot" }
        }
        val methods = linkedMapOf<String, Method>()
        plan.hooks.forEach { hook ->
            val definingLoader = layout.bindings.getValue(hook.source.loaderId)
            val method = TargetMethodResolver.resolve(definingLoader, hook.descriptor, hook.shape)
            require(method.declaringClass.classLoader === definingLoader) { "Method was defined by another loader: ${hook.descriptor}" }
            methods[hook.role] = method
        }
        return Collections.unmodifiableMap(methods)
    }
}
