package io.github.nku100.impad.compat.code

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNotEquals
import kotlin.test.assertNull

class CodeSnapshotTest {
    private val hashA = "a".repeat(64)
    private val hashB = "b".repeat(64)

    @Test
    fun `resolution honors parent-first and delegate-last`() {
        val parentFirst = snapshot(CodeDelegation.PARENT_FIRST)
        val delegateLast = snapshot(CodeDelegation.DELEGATE_LAST)

        assertEquals(listOf("parent", "ready"), parentFirst.resolutionOrder())
        assertEquals(listOf("ready", "parent"), delegateLast.resolutionOrder())
    }

    @Test
    fun `first DEX in winning loader owns the whole class`() {
        val snap = CodeSnapshot("ready", listOf(
            loader("parent", null, CodeDelegation.PARENT_FIRST, hashA),
            loader("ready", "parent", CodeDelegation.PARENT_FIRST, hashB, hashA),
        ))
        val index = EffectiveClassIndex(snap, mapOf(
            DexLocation("parent", 0) to setOf("LShared;", "LParent;"),
            DexLocation("ready", 0) to setOf("LShared;", "LReady;"),
            DexLocation("ready", 1) to setOf("LShared;", "LSecond;"),
        ))

        assertEquals(DexLocation("parent", 0), index.ownerOf("LShared;"))
        assertEquals(DexLocation("ready", 0), index.ownerOf("LReady;"))
        assertNull(index.ownerOf("LMissing;"))
        assertEquals(DexLocation("parent", 0), index.ownerOf("LShared;", "ready"))
    }

    @Test
    fun `delegate-last child definition wins and empty definition sets are valid`() {
        val snap = CodeSnapshot("ready", listOf(
            loader("parent", null, CodeDelegation.PARENT_FIRST, hashA),
            loader("ready", "parent", CodeDelegation.DELEGATE_LAST, hashB, hashA),
        ))
        val index = EffectiveClassIndex(snap, mapOf(
            DexLocation("parent", 0) to setOf("LShared;"),
            DexLocation("ready", 0) to setOf("LShared;"),
            DexLocation("ready", 1) to emptySet(),
        ))
        assertEquals(DexLocation("ready", 0), index.ownerOf("LShared;"))
    }

    @Test
    fun `content identity includes topology and ordered DEX hashes`() {
        val base = snapshot(CodeDelegation.PARENT_FIRST)
        val changedTopology = CodeSnapshot("ready", listOf(
            loader("parent", "middle", CodeDelegation.PARENT_FIRST, hashA),
            loader("middle", null, CodeDelegation.PARENT_FIRST, hashA),
            loader("ready", "parent", CodeDelegation.PARENT_FIRST, hashB),
        ))
        val changedDexOrder = CodeSnapshot("ready", listOf(
            loader("parent", null, CodeDelegation.PARENT_FIRST, hashA),
            loader("ready", "parent", CodeDelegation.PARENT_FIRST, hashA, hashB),
        ))
        assertNotEquals(base.contentSha256(), changedTopology.contentSha256())
        assertNotEquals(base.contentSha256(), changedDexOrder.contentSha256())
        assertEquals(base.contentSha256(), snapshot(CodeDelegation.PARENT_FIRST, hashB.uppercase()).contentSha256())
    }

    @Test
    fun `identity encoding is unambiguous`() {
        val first = CodeSnapshot("a", listOf(loader("a", null, CodeDelegation.PARENT_FIRST, hashA)))
        val second = CodeSnapshot("a", listOf(loader("a", null, CodeDelegation.PARENT_FIRST, hashA)))
        assertEquals(first.contentSha256(), second.contentSha256())
    }

    @Test
    fun `rejects invalid loader topology and protocol`() {
        assertFailsWith<IllegalArgumentException> { CodeSnapshot("x", emptyList()) }
        assertFailsWith<IllegalArgumentException> { CodeSnapshot("x", listOf(loader(" ", null, CodeDelegation.PARENT_FIRST, hashA))) }
        assertFailsWith<IllegalArgumentException> { CodeSnapshot("x", listOf(loader("x", null, CodeDelegation.PARENT_FIRST, hashA), loader("x", null, CodeDelegation.PARENT_FIRST, hashA))) }
        assertFailsWith<IllegalArgumentException> { CodeSnapshot("x", listOf(loader("x", "missing", CodeDelegation.PARENT_FIRST, hashA))) }
        assertFailsWith<IllegalArgumentException> { CodeSnapshot("x", listOf(loader("x", "y", CodeDelegation.PARENT_FIRST, hashA), loader("y", "x", CodeDelegation.PARENT_FIRST, hashA))) }
        assertFailsWith<IllegalArgumentException> { CodeSnapshot("x", listOf(loader("x", null, CodeDelegation.PARENT_FIRST, hashA), loader("orphan", null, CodeDelegation.PARENT_FIRST, hashA))) }
        assertFailsWith<IllegalArgumentException> { CodeSnapshot("x", listOf(loader("x", null, CodeDelegation.PARENT_FIRST, "bad"))) }
        assertFailsWith<IllegalArgumentException> { CodeSnapshot("x", listOf(loader("x", null, CodeDelegation.PARENT_FIRST)), protocolVersion = 2) }
        assertFailsWith<IllegalArgumentException> { CodeSnapshot("x", listOf(loader("x", null, CodeDelegation.PARENT_FIRST))) }
    }

    @Test
    fun `index rejects missing and extra DEX locations`() {
        val snap = snapshot(CodeDelegation.PARENT_FIRST)
        assertFailsWith<IllegalArgumentException> {
            EffectiveClassIndex(snap, mapOf(DexLocation("ready", 0) to emptySet()))
        }
        assertFailsWith<IllegalArgumentException> {
            EffectiveClassIndex(snap, mapOf(
                DexLocation("parent", 0) to emptySet(),
                DexLocation("ready", 0) to emptySet(),
                DexLocation("unknown", 0) to emptySet(),
            ))
        }
    }

    @Test
    fun `snapshot and index defensively copy caller collections`() {
        val hashes = mutableListOf(hashA)
        val sourceLoader = CodeLoader("ready", null, CodeDelegation.PARENT_FIRST, hashes)
        val sourceLoaders = mutableListOf(sourceLoader)
        val snap = CodeSnapshot("ready", sourceLoaders)
        val names = mutableSetOf("LBefore;")
        val sourceDefinitions = mutableMapOf(DexLocation("ready", 0) to names)
        val index = EffectiveClassIndex(snap, sourceDefinitions)
        val originalHash = snap.contentSha256()

        hashes[0] = hashB
        sourceLoaders.clear()
        names.clear()
        sourceDefinitions.clear()

        assertEquals(originalHash, snap.contentSha256())
        assertEquals(listOf(hashA), snap.loaders.single().dexSha256)
        assertEquals(DexLocation("ready", 0), index.ownerOf("LBefore;"))
        assertFailsWith<UnsupportedOperationException> { (snap.loaders as MutableList).clear() }
    }

    @Test
    fun `precomputed class ownership stays namespace-specific after input mutation`() {
        val sourceLoaders = mutableListOf(
            loader("root", null, CodeDelegation.PARENT_FIRST, hashA),
            loader("middle", "root", CodeDelegation.PARENT_FIRST, hashB),
            loader("leaf", "middle", CodeDelegation.DELEGATE_LAST, hashA),
        )
        val snap = CodeSnapshot("leaf", sourceLoaders)
        val rootClasses = mutableSetOf("LShared;", "LRoot;")
        val middleClasses = mutableSetOf("LShared;", "LMiddle;")
        val leafClasses = mutableSetOf("LShared;", "LLeaf;")
        val sourceDefinitions = mutableMapOf(
            DexLocation("root", 0) to rootClasses,
            DexLocation("middle", 0) to middleClasses,
            DexLocation("leaf", 0) to leafClasses,
        )
        val index = EffectiveClassIndex(snap, sourceDefinitions)

        sourceLoaders.clear()
        rootClasses.clear()
        middleClasses.clear()
        leafClasses.clear()
        sourceDefinitions.clear()

        assertEquals(DexLocation("root", 0), index.ownerOf("LShared;", "root"))
        assertEquals(DexLocation("root", 0), index.ownerOf("LShared;", "middle"))
        assertEquals(DexLocation("leaf", 0), index.ownerOf("LShared;", "leaf"))
        assertEquals(DexLocation("middle", 0), index.ownerOf("LMiddle;", "leaf"))
        assertNull(index.ownerOf("LUnknown;", "leaf"))
    }

    private fun snapshot(delegation: CodeDelegation, readyHash: String = hashB) = CodeSnapshot(
        "ready",
        listOf(
            loader("parent", null, CodeDelegation.PARENT_FIRST, hashA),
            loader("ready", "parent", delegation, readyHash),
        ),
    )

    private fun loader(id: String, parent: String?, delegation: CodeDelegation, vararg hashes: String) =
        CodeLoader(id, parent, delegation, hashes.toList())
}
