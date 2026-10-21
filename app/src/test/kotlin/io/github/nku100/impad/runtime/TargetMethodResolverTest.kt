package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.rules.MethodShape
import java.net.URLClassLoader
import org.junit.jupiter.api.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class TargetMethodResolverTest {
    @Test
    fun resolvesObjectPrimitiveAndArrayDescriptors() {
        val descriptor = "L${DescriptorFixture::class.java.name.replace('.', '/')};" +
            "->sample(Ljava/lang/Object;I[Ljava/lang/String;[Z)Ljava/util/List;"

        val method = TargetMethodResolver.resolve(javaClass.classLoader!!, descriptor)

        assertEquals("sample", method.name)
        assertEquals(
            listOf(Any::class.java, Int::class.javaPrimitiveType, Array<String>::class.java, BooleanArray::class.java),
            method.parameterTypes.toList(),
        )
        assertEquals(List::class.java, method.returnType)
    }

    @Test
    fun resolvesPrimitiveReturnDescriptors() {
        val descriptor = "L${DescriptorFixture::class.java.name.replace('.', '/')};->count(J)D"

        val method = TargetMethodResolver.resolve(javaClass.classLoader!!, descriptor)

        assertEquals(Long::class.javaPrimitiveType, method.parameterTypes.single())
        assertEquals(Double::class.javaPrimitiveType, method.returnType)
    }

    @Test
    fun rejectsAMissingMethod() {
        val descriptor = "L${DescriptorFixture::class.java.name.replace('.', '/')};->missing()V"

        assertFailsWith<IllegalArgumentException> {
            TargetMethodResolver.resolve(javaClass.classLoader!!, descriptor)
        }
    }

    @Test
    fun validatesTheCompleteShapeAndStaticness() {
        val descriptor = "L${DescriptorFixture::class.java.name.replace('.', '/')};->count(J)D"

        assertFailsWith<IllegalArgumentException> {
            TargetMethodResolver.resolve(
                javaClass.classLoader!!,
                descriptor,
                MethodShape(listOf("long"), "double", true),
            )
        }
        assertFailsWith<IllegalArgumentException> {
            TargetMethodResolver.resolve(
                javaClass.classLoader!!,
                descriptor,
                MethodShape(listOf("int"), "double", true),
            )
        }
    }

    @Test
    fun acceptsTargetClassesFromTheRequestedLoaderOrItsParents() {
        val childLoader = object : ClassLoader(javaClass.classLoader) {}
        val descriptor = "L${DescriptorFixture::class.java.name.replace('.', '/')};->count(J)D"

        val method = TargetMethodResolver.resolve(
            childLoader,
            descriptor,
            MethodShape(listOf("long"), "double", false),
        )

        assertEquals(DescriptorFixture::class.java.classLoader, method.declaringClass.classLoader)
    }

    @Test
    fun rejectsATargetReturnedFromAnUnrelatedLoader() {
        val binaryName = DescriptorFixture::class.java.name
        val descriptor = "L${binaryName.replace('.', '/')};->count(J)D"
        val fixtureLocation = checkNotNull(DescriptorFixture::class.java.protectionDomain?.codeSource?.location)
        URLClassLoader(arrayOf(fixtureLocation), null).use { foreign ->
            val readyLoader = object : ClassLoader(null) {
                override fun loadClass(name: String): Class<*> =
                    if (name == binaryName) foreign.loadClass(name) else super.loadClass(name)
            }

            assertFailsWith<IllegalArgumentException> {
                TargetMethodResolver.resolve(
                    readyLoader,
                    descriptor,
                    MethodShape(listOf("long"), "double", false),
                )
            }
        }
    }

    class DescriptorFixture {
        fun sample(value: Any, count: Int, names: Array<String>, flags: BooleanArray): List<String> =
            if (value.hashCode() >= 0 && count >= 0 && flags.isNotEmpty()) names.toList() else emptyList()

        fun count(value: Long): Double = value.toDouble()
    }
}
