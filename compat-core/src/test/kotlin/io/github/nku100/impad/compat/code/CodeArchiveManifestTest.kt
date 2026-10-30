package io.github.nku100.impad.compat.code

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class CodeArchiveManifestTest {
    @Test
    fun freezesOrderedDexContentListAndAllowsResourceOnlyZip() {
        val hashes = mutableListOf("a".repeat(64), "b".repeat(64))
        val manifest = CodeArchiveManifest("root", 0, CodeContainerKind.ZIP, "c".repeat(64), hashes)
        hashes.clear()
        assertEquals(listOf("a".repeat(64), "b".repeat(64)), manifest.dexSha256)
        assertFailsWith<UnsupportedOperationException> { (manifest.dexSha256 as MutableList).clear() }
        CodeArchiveManifest("root", 1, CodeContainerKind.ZIP, "c".repeat(64), emptyList())
    }

    @Test
    fun rawDexMustBeOneEntryWithTheSameContentIdentity() {
        CodeArchiveManifest("root", 0, CodeContainerKind.DEX, "a".repeat(64), listOf("A".repeat(64)))
        for (hashes in listOf(emptyList(), listOf("b".repeat(64)), listOf("a".repeat(64), "a".repeat(64)))) {
            assertFailsWith<IllegalArgumentException> { CodeArchiveManifest("root", 0, CodeContainerKind.DEX, "a".repeat(64), hashes) }
        }
        assertFailsWith<IllegalArgumentException> { CodeArchiveManifest("root", -1, CodeContainerKind.ZIP, "a".repeat(64), emptyList()) }
        assertFailsWith<IllegalArgumentException> { CodeArchiveManifest("root", 0, CodeContainerKind.ZIP, "invalid", emptyList()) }
    }
}
