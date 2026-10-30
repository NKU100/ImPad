package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.code.*
import io.github.nku100.impad.compat.rules.*
import io.github.nku100.impad.rules.CodeLoaderInput
import io.github.nku100.impad.rules.SemanticAppCatalogs
import java.io.File
import kotlin.test.*

class SemanticPlanBinderTest {
    private val catalog = SemanticAppCatalogs.forApp("qq")
    private val parent = Fixture::class.java.classLoader!!
    private val child = object : ClassLoader(parent) {
        override fun loadClass(name: String): Class<*> =
            if (name == Parameter::class.java.name) String::class.java else super.loadClass(name)
    }
    private val hash = "a".repeat(64)
    private val snapshot = CodeSnapshot("child", listOf(CodeLoader("child", "parent", CodeDelegation.PARENT_FIRST, listOf(hash)), CodeLoader("parent", null, CodeDelegation.PARENT_FIRST, listOf(hash))))
    private val layout = StartupLoaderLayout("child", listOf(CodeLoaderInput("child", "parent", CodeDelegation.PARENT_FIRST, listOf(File("child.apk"))), CodeLoaderInput("parent", null, CodeDelegation.PARENT_FIRST, listOf(File("parent.apk")))), mapOf("child" to child, "parent" to parent))
    private fun plan(source: String = "parent", shape: MethodShape = MethodShape(listOf(Parameter::class.java.name), "boolean", false)): CompletePlan {
        val strategy = catalog.strategies.single()
        val role = strategy.roles.single()
        val descriptor = "L${Fixture::class.java.name.replace('.', '/')};->get(L${Parameter::class.java.name.replace('.', '/')};)Z"
        return CompletePlan(snapshot.contentSha256(), catalog.contractId, catalog.contractRevision, strategy.id, strategy.revision,
            listOf(PlanHook(role.role, descriptor, shape, TargetCodeSource(source, 0, hash), role.phase, role.configuration)))
    }
    @Test
    fun bindsParentDefinedParametersInTheirDeclaringNamespace() {
        val methods = SemanticPlanBinder.bind(plan(), snapshot, layout, catalog)
        val method = methods.getValue("device_type")
        assertSame(parent, method.declaringClass.classLoader)
        assertEquals(listOf(Parameter::class.java), method.parameterTypes.toList())
        assertFailsWith<UnsupportedOperationException> { (methods as MutableMap).clear() }
    }
    @Test
    fun refusesAnAncestorDefinitionClaimedAsAChildDefinition() {
        assertFailsWith<IllegalArgumentException> { SemanticPlanBinder.bind(plan("child"), snapshot, layout, catalog) }
    }
    @Test
    fun refusesChangedShapeOrLoaderTopology() {
        assertFailsWith<IllegalArgumentException> { SemanticPlanBinder.bind(plan(shape = MethodShape(emptyList(), "boolean", true)), snapshot, layout, catalog) }
        val wrong = CodeSnapshot("child", listOf(CodeLoader("child", "parent", CodeDelegation.DELEGATE_LAST, listOf(hash)), CodeLoader("parent", null, CodeDelegation.PARENT_FIRST, listOf(hash))))
        val old = plan()
        val changed = CompletePlan(wrong.contentSha256(), old.contractId, old.contractRevision, old.strategyId, old.strategyRevision, old.hooks)
        assertFailsWith<IllegalArgumentException> { SemanticPlanBinder.bind(changed, wrong, layout, catalog) }
    }
    class Parameter
    class Fixture { fun get(value: Parameter): Boolean = value.hashCode() != 0 }
}
