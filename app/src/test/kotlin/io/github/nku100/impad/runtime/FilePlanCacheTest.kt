package io.github.nku100.impad.runtime

import java.io.File
import java.nio.file.Files
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNull
import kotlin.test.assertTrue

class FilePlanCacheTest {
    @Test
    fun roundTripsOpaqueUtf8PayloadAndNormalizesKeyCase() = withCache { root, cache ->
        val payload = "方案📱\nopaque\u0000bytes"
        cache.write("qq_rules", KEY.uppercase(), payload)

        assertEquals(payload, cache.read("qq_rules", KEY))
        assertEquals(1, root.walkTopDown().count { it.isFile })
        assertTrue(root.walkTopDown().filter { it.isFile }.all { it.name.matches(Regex("[a-z0-9_-]+-[0-9a-f]+\\.json")) })
    }

    @Test
    fun malformedTruncatedWrongKeySchemaAndChecksumEntriesAreCacheMisses() = withCache { root, cache ->
        fun corrupt(replacement: (String) -> String) {
            cache.write("qq", KEY, "valid payload")
            val file = cacheFile(root)
            file.writeText(replacement(file.readText()), Charsets.UTF_8)
            assertNull(cache.read("qq", KEY))
        }

        corrupt { "{" }
        corrupt { it.take(it.length / 2) }
        corrupt { it.replace(KEY, OTHER_KEY) }
        corrupt { it.replace("\"schemaVersion\":1", "\"schemaVersion\":2") }
        corrupt { it.replace(sha256("valid payload"), "0".repeat(64)) }
    }

    @Test
    fun namespacesAndKeysHaveIndependentEntries() = withCache { root, cache ->
        cache.write("qq", KEY, "qq first")
        cache.write("qq", OTHER_KEY, "qq second")
        cache.write("wecom", KEY, "wecom first")

        assertEquals("qq first", cache.read("qq", KEY))
        assertEquals("qq second", cache.read("qq", OTHER_KEY))
        assertEquals("wecom first", cache.read("wecom", KEY))
    }

    @Test
    fun invalidatingOneExactKeyLeavesOtherKeysAndNamespaces() = withCache { root, cache ->
        cache.write("qq", KEY, "remove")
        cache.write("qq", OTHER_KEY, "keep key")
        cache.write("wecom", KEY, "keep namespace")

        cache.invalidate("qq", KEY)

        assertNull(cache.read("qq", KEY))
        assertEquals("keep key", cache.read("qq", OTHER_KEY))
        assertEquals("keep namespace", cache.read("wecom", KEY))
        assertEquals(2, root.walkTopDown().count { it.isFile })
    }

    @Test
    fun invalidArgumentsAreRejectedBeforeFilesystemAccess() = withCache { root, cache ->
        assertFailsWith<IllegalArgumentException> { cache.read("../escape", KEY) }
        assertFailsWith<IllegalArgumentException> { cache.write("", KEY, "payload") }
        assertFailsWith<IllegalArgumentException> { cache.invalidate("qq", "../$KEY") }
        assertFailsWith<IllegalArgumentException> { cache.read("qq", "f".repeat(63)) }
        assertTrue(!root.exists())
    }

    private fun cacheFile(root: File): File = root.walkTopDown().single { it.isFile }

    private fun withCache(block: (File, FilePlanCache) -> Unit) {
        val root = Files.createTempDirectory("impad-file-plan-cache-test-").toFile()
        root.delete()
        try {
            block(root, FilePlanCache(root))
        } finally {
            root.deleteRecursively()
        }
    }

    private fun sha256(value: String): String = java.security.MessageDigest.getInstance("SHA-256")
        .digest(value.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }

    private companion object {
        val KEY = "a".repeat(64)
        val OTHER_KEY = "b".repeat(64)
    }
}
