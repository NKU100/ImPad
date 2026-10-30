package io.github.nku100.impad.runtime

import java.io.File
import java.nio.file.Files
import java.nio.file.LinkOption
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class StartupRuntimeSnapshotTest {
    @Test
    fun bindsToExpectedDeclaringLoaderAndRejectsUnrelatedLoader() = withFixture { source, storage ->
        val ready = Loader()
        val reader = StartupLoaderLayoutReader { StartupLoaderDescription("dalvik.system.PathClassLoader", null, listOf(source)) }
        StartupRuntimeSnapshot.capture(ready, storage, reader).use { runtime ->
            val name = runtime.snapshot.dexes.first().classNames.first()
            runtime.requireDeclaringLoader(name, ready)
            assertFailsWith<IllegalArgumentException> { runtime.requireDeclaringLoader(name, Loader()) }
            assertFailsWith<IllegalArgumentException> { runtime.requireDeclaringLoader("unknown.Class", ready) }
            assertTrue(runtime.inputsStillCurrent())
            source.appendBytes(byteArrayOf(0))
            assertFalse(runtime.inputsStillCurrent())
        }
        assertOnlyLifecycleLockRemains(storage)
    }

    @Test
    fun layoutChangeDuringCaptureDeletesPrivateCopies() = withFixture { source, storage ->
        val ready = Loader()
        val another = File(source.parentFile, "other.apk").also { source.copyTo(it) }
        var reads = 0
        val reader = StartupLoaderLayoutReader {
            StartupLoaderDescription("dalvik.system.PathClassLoader", null, listOf(if (++reads == 1) source else another))
        }
        assertFailsWith<IllegalStateException> { StartupRuntimeSnapshot.capture(ready, storage, reader) }
        assertOnlyLifecycleLockRemains(storage)
    }

    private fun assertOnlyLifecycleLockRemains(storage: File) {
        val lifecycleLock = File(storage, ".impad-code-lifecycle.lock")
        assertTrue(Files.isRegularFile(lifecycleLock.toPath(), LinkOption.NOFOLLOW_LINKS))
        val remainingEntries = storage.listFiles().orEmpty()
        assertEquals(setOf(lifecycleLock.name), remainingEntries.map { it.name }.toSet())
        assertTrue(remainingEntries.none { it.isDirectory }, "Snapshot directories must be removed")
        assertTrue(storage.walkTopDown().none { it != storage && it.extension == "dex" },
            "Private DEX copies must be removed")
    }

    private fun withFixture(block: (File, File) -> Unit) {
        val root = Files.createTempDirectory("runtime-snapshot-test-").toFile()
        try {
            val fixture = listOf(File("../compat-core/src/test/resources/multidex-fixture.apk"),
                File("compat-core/src/test/resources/multidex-fixture.apk")).first { it.isFile }
            val source = File(root, "base.apk").also { fixture.copyTo(it) }
            val storage = File(root, "snapshots").apply { mkdirs() }
            block(source, storage)
        } finally { root.deleteRecursively() }
    }

    private class Loader : ClassLoader(null)
}
