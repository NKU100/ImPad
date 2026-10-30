package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.code.CodeDelegation
import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class StartupLoaderLayoutReaderTest {
    private val boot = Loader(null)
    private val parent = Loader(boot)
    private val child = Loader(parent)
    private val a = File("base.apk").canonicalFile
    private val b = File("patch.apk").canonicalFile
    private val c = File("split.apk").canonicalFile
    private val nodes = mutableMapOf<ClassLoader, StartupLoaderDescription>(
        boot to StartupLoaderDescription("java.lang.BootClassLoader", null, emptyList()),
        parent to StartupLoaderDescription("dalvik.system.PathClassLoader", boot, listOf(a)),
        child to StartupLoaderDescription("dalvik.system.DelegateLastClassLoader", parent, listOf(b, c)),
    )
    private val reader = StartupLoaderLayoutReader { nodes.getValue(it) }

    @Test
    fun capturesEveryFileAndModelsDelegationWithoutApplicationNames() {
        val layout = reader.read(child)
        assertEquals("loader-0", layout.readyLoaderId)
        assertEquals(listOf("loader-0", "loader-1"), layout.loaders.map { it.id })
        assertEquals(listOf(b, c), layout.loaders.first().archives)
        assertEquals(CodeDelegation.DELEGATE_LAST, layout.loaders.first().delegation)
        assertEquals(CodeDelegation.PARENT_FIRST, layout.loaders.last().delegation)
        assertEquals("loader-1", layout.loaders.first().parentId)
        assertEquals(null, layout.loaders.last().parentId)
        assertTrue(layout.bindings["loader-0"] === child)
        assertTrue(layout.bindings["loader-1"] === parent)
        assertEquals(2, layout.bindings.size)
    }

    @Test
    fun dexClassLoaderUsesParentFirstOrder() {
        nodes[child] = StartupLoaderDescription("dalvik.system.DexClassLoader", parent, listOf(b))
        assertEquals(CodeDelegation.PARENT_FIRST, reader.read(child).loaders.first().delegation)
    }

    @Test
    fun unknownCustomLoaderIsRejectedEvenWithReadablePaths() {
        nodes[child] = StartupLoaderDescription("custom.InjectionLoader", parent, listOf(b))
        assertFailsWith<IllegalArgumentException> { reader.read(child) }
    }

    @Test
    fun bootReadyAndEntirelyEmptyInputsAreRejected() {
        assertFailsWith<IllegalArgumentException> { reader.read(boot) }
        nodes[parent] = StartupLoaderDescription("dalvik.system.PathClassLoader", boot, emptyList())
        assertFailsWith<IllegalArgumentException> { reader.read(parent) }
    }

    @Test
    fun layoutChangeIncludesOrderingAndObjectIdentity() {
        val first = reader.read(child)
        assertTrue(first.sameAs(reader.read(child)))
        nodes[child] = nodes.getValue(child).copy(dexFiles = listOf(c, b))
        assertFalse(first.sameAs(reader.read(child)))
        nodes[child] = nodes.getValue(child).copy(dexFiles = listOf(b, c))
        val anotherChild = Loader(parent)
        nodes[anotherChild] = nodes.getValue(child)
        assertFalse(first.sameAs(reader.read(anotherChild)))
    }

    @Test
    fun parentCycleIsRejected() {
        nodes[parent] = nodes.getValue(parent).copy(parent = child)
        assertFailsWith<IllegalArgumentException> { reader.read(child) }
    }

    private class Loader(parent: ClassLoader?) : ClassLoader(parent)
}
