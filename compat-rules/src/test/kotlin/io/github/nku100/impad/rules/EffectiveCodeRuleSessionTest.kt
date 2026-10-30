package io.github.nku100.impad.rules

import io.github.nku100.impad.compat.code.CodeDelegation
import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

class EffectiveCodeRuleSessionTest {
    @Test
    fun missingMethodInWinningClassDoesNotReviveOldDefinition() = withCodeDirectory { root ->
        val files = listOf(codeArchive(root, "new.apk", listOf("classes.dex" to "sample.Target")),
            codeArchive(root, "old.apk", listOf("classes.dex" to "sample.Target")))
        capture(files).use { snapshot ->
            var next = 0
            EffectiveCodeRuleSession.open(snapshot) { Backend(next++) }.use { session ->
                assertEquals(listOf("Lsample/Target;->newOnly()V"), session.findMethods(MethodQuery()).map { it.descriptor })
                assertTrue(session.findMethods(MethodQuery(requiredStrings = setOf("old"))).isEmpty())
                assertEquals(listOf("field0"), session.findClasses("sample.Target").single().fields.map { it.name })
            }
        }
    }

    @Test
    fun methodsClassesAndInstructionsUseSameDelegationWinner() = withCodeDirectory { root ->
        val parent = codeArchive(root, "parent.apk", listOf("classes.dex" to "sample.Target"))
        val child = codeArchive(root, "child.apk", listOf("classes.dex" to "sample.Target"))
        for ((delegation, expected) in listOf(CodeDelegation.PARENT_FIRST to 0, CodeDelegation.DELEGATE_LAST to 1)) {
            StartupCodeSnapshot().capture("child", listOf(
                CodeLoaderInput("parent", null, CodeDelegation.PARENT_FIRST, listOf(parent)),
                CodeLoaderInput("child", "parent", delegation, listOf(child)),
            )).use { snapshot ->
                var next = 0
                EffectiveCodeRuleSession.open(snapshot) { Backend(next++) }.use { session ->
                    val method = session.findMethods(MethodQuery()).single()
                    assertEquals(expected, method.sourceDexIndex)
                    assertEquals("field$expected", session.findClasses("sample.Target").single().fields.single().name)
                    assertEquals("SOURCE_$expected", session.readInstructions(method).single().opcode)
                    assertFailsWith<IllegalArgumentException> { session.readInstructions(method.copy(sourceDexIndex = 1 - expected)) }
                }
            }
        }
    }

    @Test
    fun backendOpenFailureClosesOnlyOpenedBackendsAndDoesNotDeleteSnapshot() = withCodeDirectory { root ->
        val files = listOf(codeArchive(root, "a.apk", listOf("classes.dex" to "sample.Target")),
            codeArchive(root, "b.apk", listOf("classes.dex" to "sample.Other")))
        capture(files).use { snapshot ->
            val closed = mutableListOf<Int>()
            var next = 0
            assertFailsWith<IllegalStateException> {
                EffectiveCodeRuleSession.open(snapshot) {
                    val current = next++
                    if (current == 1) error("open failed")
                    Backend(current, closed)
                }
            }
            assertEquals(listOf(0), closed)
            assertTrue(snapshot.dexes.all { it.file.isFile })
        }
    }

    @Test
    fun closingSessionClosesBackendsInReverseOrderAndRejectsQueries() = withCodeDirectory { root ->
        val files = listOf(codeArchive(root, "a.apk", listOf("classes.dex" to "sample.Target")),
            codeArchive(root, "b.apk", listOf("classes.dex" to "sample.Other")))
        capture(files).use { snapshot ->
            val closed = mutableListOf<Int>()
            var next = 0
            val session = EffectiveCodeRuleSession.open(snapshot) { Backend(next++, closed) }
            session.close(); session.close()
            assertEquals(listOf(1, 0), closed)
            assertTrue(snapshot.dexes.all { it.file.exists() })
            assertFailsWith<IllegalStateException> { session.findMethods(MethodQuery()) }
            assertFailsWith<IllegalStateException> { session.findClasses("sample.Target") }
            assertFailsWith<IllegalStateException> { session.readInstructions(method(0)) }
        }
    }

    @Test
    fun closedBorrowedSnapshotIsRejected() = withCodeDirectory { root ->
        val source = codeArchive(root, "a.apk", listOf("classes.dex" to "sample.Target"))
        val snapshot = capture(listOf(source))
        val session = EffectiveCodeRuleSession.open(snapshot) { Backend(0) }
        snapshot.close()
        try { assertFailsWith<IllegalStateException> { session.findMethods(MethodQuery()) } } finally { session.close() }
        assertFailsWith<IllegalStateException> { EffectiveCodeRuleSession.open(snapshot) { Backend(0) } }
    }

    @Test
    fun descriptorCannotBeRetargetedToShadowedClassInSameDex() = withCodeDirectory { root ->
        val files = listOf(codeArchive(root, "new.apk", listOf("classes.dex" to "sample.Target")),
            codeArchiveWithClasses(root, "old.apk", listOf("classes.dex" to listOf("sample.Target", "sample.Visible"))))
        capture(files).use { snapshot ->
            var next = 0
            EffectiveCodeRuleSession.open(snapshot) {
                val index = next++
                object : RuleSessionBackend {
                    override fun findMethods(query: MethodQuery) = if (index == 1)
                        listOf(method(index).copy(className = "sample.Visible", descriptor = "Lsample/Visible;->safe()V")) else emptyList()
                    override fun findClasses(typeName: String) = emptyList<RuleClass>()
                    override fun readInstructions(descriptor: String) = listOf(RuleInstruction(0, "SHADOWED_OLD_METHOD"))
                    override fun close() = Unit
                }
            }.use { session ->
                val visible = session.findMethods(MethodQuery()).single()
                assertFailsWith<IllegalArgumentException> {
                    session.readInstructions(visible.copy(descriptor = "Lsample/Target;->oldOnly()V"))
                }
            }
        }
    }

    @Test
    fun methodFromAnotherSessionIsRejectedEvenForIdenticalSnapshot() = withCodeDirectory { root ->
        val file = codeArchive(root, "a.apk", listOf("classes.dex" to "sample.Target"))
        capture(listOf(file)).use { snapshot ->
            val stale = EffectiveCodeRuleSession.open(snapshot) { Backend(0) }.use { it.findMethods(MethodQuery()).single() }
            EffectiveCodeRuleSession.open(snapshot) { Backend(0) }.use { current ->
                assertFailsWith<IllegalArgumentException> { current.readInstructions(stale) }
                assertEquals("SOURCE_0", current.readInstructions(current.findMethods(MethodQuery()).single()).single().opcode)
            }
        }
    }

    @Test
    fun sourceIdentityComesFromOwnedMethodAndSnapshotMetadata() = withCodeDirectory { root ->
        val file = codeArchive(root, "a.apk", listOf("classes.dex" to "sample.Target"))
        capture(listOf(file)).use { snapshot ->
            EffectiveCodeRuleSession.open(snapshot) { Backend(0) }.use { session ->
                val method = session.findMethods(MethodQuery()).single()
                val source = session.sourceOf(method)
                assertEquals("app", source.loaderId)
                assertEquals(0, source.dexIndex)
                assertEquals(snapshot.metadata.loaders.single().dexSha256.single(), source.dexSha256)
                assertFailsWith<IllegalArgumentException> { session.sourceOf(method.copy(descriptor = "Lsample/Target;->wrong()V")) }
            }
        }
    }

    @Test
    fun parentMethodsResolveDependenciesInTheirOwnLoaderNamespace() = withCodeDirectory { root ->
        val parent = codeArchiveWithClasses(root, "parent.apk", listOf("classes.dex" to listOf("sample.Parent", "sample.Target")))
        val child = codeArchive(root, "child.apk", listOf("classes.dex" to "sample.Target"))
        StartupCodeSnapshot().capture("child", listOf(
            CodeLoaderInput("parent", null, CodeDelegation.PARENT_FIRST, listOf(parent)),
            CodeLoaderInput("child", "parent", CodeDelegation.DELEGATE_LAST, listOf(child)),
        )).use { snapshot ->
            var next = 0
            val closed = mutableListOf<Int>()
            EffectiveCodeRuleSession.open(snapshot) {
                val index = next++
                object : RuleSessionBackend {
                    override fun findMethods(query: MethodQuery) = listOfNotNull(
                        method(index), if (index == 0) method(0).copy(className = "sample.Parent", descriptor = "Lsample/Parent;->run()V") else null,
                    ).filter { query.declaredClass == null || it.className == query.declaredClass }
                    override fun findClasses(typeName: String) = emptyList<RuleClass>()
                    override fun readInstructions(descriptor: String) = emptyList<RuleInstruction>()
                    override fun close() { closed += index }
                }
            }.use { session ->
                val rootTarget = session.findMethods(MethodQuery(declaredClass = "sample.Target")).single()
                assertEquals("child", session.sourceOf(rootTarget).loaderId)
                val parentMethod = session.findMethods(MethodQuery(declaredClass = "sample.Parent")).single()
                val scoped = session.inContextOf(parentMethod)
                val dependency = scoped.findMethods(MethodQuery(declaredClass = "sample.Target")).single()
                assertEquals("parent", scoped.sourceOf(dependency).loaderId)
                assertEquals("parent", session.sourceOf(dependency).loaderId)
                assertFailsWith<IllegalArgumentException> { session.readInstructions(dependency) }
                assertFailsWith<IllegalArgumentException> { scoped.inNamespace("child") }
                session.inNamespace("parent").use { byId ->
                    assertEquals("parent", byId.sourceOf(byId.findMethods(MethodQuery(declaredClass = "sample.Target")).single()).loaderId)
                }
                scoped.close()
                assertTrue(closed.isEmpty())
                assertTrue(session.findMethods(MethodQuery()).isNotEmpty())
                assertFailsWith<IllegalStateException> { scoped.findMethods(MethodQuery()) }
                val anotherScope = session.inContextOf(parentMethod)
                session.close()
                assertFailsWith<IllegalStateException> { anotherScope.findMethods(MethodQuery()) }
            }
            assertEquals(listOf(1, 0), closed)
        }
    }

    private fun capture(files: List<File>) = StartupCodeSnapshot().capture("app", listOf(
        CodeLoaderInput("app", null, CodeDelegation.PARENT_FIRST, files),
    ))

    private class Backend(val index: Int, val closed: MutableList<Int> = mutableListOf()) : RuleSessionBackend {
        override fun findMethods(query: MethodQuery): List<RuleMethod> = listOf(method(index)).filter {
            query.requiredStrings.isEmpty() || query.requiredStrings.all(it.strings::contains)
        }
        override fun findClasses(typeName: String) = listOf(RuleClass(typeName, "java.lang.Object", listOf(RuleClassField("field$index", 0))))
        override fun readInstructions(descriptor: String) = listOf(RuleInstruction(0, "SOURCE_$index"))
        override fun close() { closed += index }
    }

    private companion object {
        fun method(index: Int) = RuleMethod("Lsample/Target;->${if (index == 0) "newOnly" else "oldOnly"}()V",
            "sample.Target", emptyList(), "void", true, 1, setOf(if (index == 0) "new" else "old"), emptyList(), emptyList())
    }
}
