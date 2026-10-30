package io.github.nku100.impad.rules

import com.android.tools.smali.dexlib2.Opcodes
import com.android.tools.smali.dexlib2.immutable.ImmutableClassDef
import com.android.tools.smali.dexlib2.immutable.ImmutableDexFile
import com.android.tools.smali.dexlib2.writer.pool.DexPool
import io.github.nku100.impad.compat.code.CodeDelegation
import io.github.nku100.impad.compat.code.DexLocation
import java.io.File
import java.io.FilterInputStream
import java.nio.channels.FileChannel
import java.nio.file.LinkOption
import java.nio.file.StandardOpenOption
import java.nio.file.Files
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertSame
import kotlin.test.assertTrue

class StartupCodeSnapshotTest {
    @Test
    fun manifestsKeepContainerPositionsAndTheirOrderedDexHashes() = withCodeDirectory { root ->
        val archive = codeArchive(root, "input.apk", listOf("classes2.dex" to "sample.B", "classes.dex" to "sample.A"))
        val resources = File(root, "resources.apk")
        ZipOutputStream(resources.outputStream()).use { }
        StartupCodeSnapshot().capture("app", listOf(CodeLoaderInput("app", null, CodeDelegation.PARENT_FIRST, listOf(resources, archive)))).use { snapshot ->
            val manifests = snapshot.archiveManifests
            assertEquals(listOf(0, 1), manifests.map { it.containerIndex })
            assertEquals(listOf("app", "app"), manifests.map { it.loaderId })
            assertTrue(manifests.first().dexSha256.isEmpty())
            assertEquals(snapshot.metadata.loaders.single().dexSha256, manifests.last().dexSha256)
            val expected = java.security.MessageDigest.getInstance("SHA-256").digest(archive.readBytes()).joinToString("") { "%02x".format(it) }
            assertEquals(expected, manifests.last().containerSha256)
            assertEquals(io.github.nku100.impad.compat.code.CodeContainerKind.ZIP, manifests.last().kind)
        }
    }

    @Test
    fun sameDexContentAtAnotherPathHasSameIdentity() = withCodeDirectory { root ->
        val a = codeArchive(root, "a.apk", listOf("classes.dex" to "sample.A"))
        val b = File(root, "b.apk").also { a.copyTo(it) }
        capture(a).use { first -> capture(b).use { second ->
            assertEquals(first.metadata.contentSha256(), second.metadata.contentSha256())
            assertEquals(setOf("sample.A"), first.dexes.single().classNames)
        } }
    }

    @Test
    fun acceptsRawFileBackedDexAndDetectsLaterContentChange() = withCodeDirectory { root ->
        val archive = codeArchive(root, "input.apk", listOf("classes.dex" to "sample.A"))
        val raw = File(root, "classes.dex")
        java.util.zip.ZipFile(archive).use { zip -> zip.getInputStream(zip.getEntry("classes.dex")).use { input ->
            raw.outputStream().use { input.copyTo(it) }
        } }
        capture(raw).use { snapshot ->
            assertEquals(setOf("sample.A"), snapshot.dexes.single().classNames)
            val manifest = snapshot.archiveManifests.single()
            assertEquals(io.github.nku100.impad.compat.code.CodeContainerKind.DEX, manifest.kind)
            assertEquals(listOf(manifest.containerSha256), manifest.dexSha256)
            assertTrue(snapshot.inputsStillCurrent())
            raw.appendBytes(byteArrayOf(0))
            assertFalse(snapshot.inputsStillCurrent())
        }
    }

    @Test
    fun normalizedDexIsStoredAndAlignedInsteadOfRecompressed() = withCodeDirectory { root ->
        val archive = codeArchive(root, "input.apk", listOf("classes.dex" to "sample.A"))
        capture(archive).use { snapshot ->
            java.util.zip.ZipFile(snapshot.dexes.single().file).use { zip ->
                val entry = zip.getEntry("classes.dex")
                assertEquals(ZipEntry.STORED, entry.method)
                assertEquals(entry.size, entry.compressedSize)
                assertEquals(0, (30 + entry.name.toByteArray().size + (entry.extra?.size ?: 0)) % 4)
            }
        }
    }

    @Test
    fun samePathWithDifferentDexContentHasDifferentIdentity() = withCodeDirectory { root ->
        val a = codeArchive(root, "a.apk", listOf("classes.dex" to "sample.A"))
        val first = capture(a).use { it.metadata.contentSha256() }
        codeArchive(root, "a.apk", listOf("classes.dex" to "sample.B"))
        capture(a).use { assertTrue(it.metadata.contentSha256() != first) }
    }

    @Test
    fun dexEntryNumberDeterminesOrderRatherThanZipInsertion() = withCodeDirectory { root ->
        val order = listOf(10, 2, 1) + (3..9)
        val archive = codeArchive(root, "multi.apk", order.map { index ->
            (if (index == 1) "classes.dex" else "classes$index.dex") to "sample.C$index"
        })
        capture(archive).use { snapshot ->
            assertEquals((1..10).map { "sample.C$it" }, snapshot.dexes.map { it.classNames.single() })
            assertEquals(DexLocation("app", 9), snapshot.definitions.ownerOf("sample.C10"))
        }
    }

    @Test
    fun gappedSecondaryDexEntriesAreRejectedRatherThanInventingLoadedDefinitions() = withCodeDirectory { root ->
        for (names in listOf(listOf("classes.dex", "classes3.dex"), listOf("classes2.dex"))) {
            val archive = codeArchive(root, "gap.apk", names.mapIndexed { index, name -> name to "sample.C$index" })
            assertFailsWith<IllegalArgumentException> { capture(archive) }
        }
    }

    @Test
    fun closingDeletesOnlyPrivateCopiesAndRejectsFurtherUse() = withCodeDirectory { root ->
        val source = codeArchive(root, "source.apk", listOf("classes.dex" to "sample.A"))
        val storage = File(root, "snapshots").apply { mkdirs() }
        val snapshot = StartupCodeSnapshot().capture("app", inputs(source), storage)
        val copies = snapshot.dexes.map { it.file }
        val ownedDirectory = copies.single().parentFile
        assertTrue(ownedDirectory.isDirectory)
        snapshot.close(); snapshot.close()
        assertTrue(source.isFile)
        assertTrue(copies.all { !it.exists() })
        assertFalse(ownedDirectory.exists())
        assertFailsWith<IllegalStateException> { snapshot.ensureOpen() }
    }

    @Test
    fun sameJvmActiveSnapshotSurvivesLaterCaptureAndBothCloseCleanTheirOwnDirectories() = withCodeDirectory { root ->
        val source = codeArchive(root, "source.apk", listOf("classes.dex" to "sample.A"))
        val storage = File(root, "snapshots").apply { mkdirs() }
        val first = StartupCodeSnapshot().capture("app", inputs(source), storage)
        val firstDirectory = first.dexes.single().file.parentFile
        val second = StartupCodeSnapshot().capture("app", inputs(source), storage)
        try {
            assertTrue(firstDirectory.isDirectory)
            assertTrue(first.dexes.single().file.isFile)
        } finally {
            second.close()
            first.close()
        }
        assertFalse(firstDirectory.exists())
        assertNoOwnedSnapshotDirectories(storage)
    }

    @Test
    fun killedProcessLeaseIsReclaimedButActiveCrossProcessLeaseIsPreserved() = withCodeDirectory { root ->
        val source = codeArchive(root, "source.apk", listOf("classes.dex" to "sample.A"))
        val storage = File(root, "snapshots").apply { mkdirs() }
        val process = startSnapshotLeaseChild(source, storage)
        val ready = process.inputStream.bufferedReader().lines().findFirst().orElse(null)
            ?: error("child process exited before publishing its lease")
        assertTrue(ready.startsWith("READY:"), "child process did not publish lease: $ready")
        val oldDirectory = File(storage, ready.removePrefix("READY:"))
        try {
            StartupCodeSnapshot().capture("app", inputs(source), storage).close()
            assertTrue(oldDirectory.isDirectory)
            assertTrue(File(oldDirectory, "dex-0.apk").isFile)
        } finally {
            process.destroyForcibly()
            assertTrue(process.waitFor(10, TimeUnit.SECONDS))
        }

        StartupCodeSnapshot().capture("app", inputs(source), storage).close()
        assertFalse(oldDirectory.exists())
        assertNoOwnedSnapshotDirectories(storage)
    }

    @Test
    fun failedCloseReleasesLeaseWithoutDeletingUntilRootLockReturns() = withCodeDirectory { root ->
        val source = codeArchive(root, "source.apk", listOf("classes.dex" to "sample.A"))
        val storage = File(root, "snapshots").apply { mkdirs() }
        val closing = StartupCodeSnapshot().capture("app", inputs(source), storage)
        val closingDirectory = closing.dexes.single().file.parentFile
        val active = StartupCodeSnapshot().capture("app", inputs(source), storage)
        val activeDirectory = active.dexes.single().file.parentFile
        val lifecycleLock = File(storage, ".impad-code-lifecycle.lock")

        assertTrue(lifecycleLock.delete())
        assertTrue(lifecycleLock.mkdir())
        try {
            assertFailsWith<IllegalArgumentException> { closing.close() }
            assertFailsWith<IllegalStateException> { closing.ensureOpen() }
            assertTrue(closingDirectory.isDirectory)

            FileChannel.open(File(closingDirectory, ".lease").toPath(), StandardOpenOption.WRITE).use { channel ->
                val lease = try { channel.tryLock() } catch (_: java.nio.channels.OverlappingFileLockException) { null }
                (lease ?: error("failed close retained the snapshot lease")).release()
            }

            assertTrue(lifecycleLock.delete())
            assertTrue(lifecycleLock.createNewFile())
            StartupCodeSnapshot().capture("app", inputs(source), storage).use { }

            assertFalse(closingDirectory.exists())
            assertTrue(activeDirectory.isDirectory)
            assertTrue(active.dexes.single().file.isFile)
        } finally {
            if (lifecycleLock.isDirectory) lifecycleLock.deleteRecursively()
            active.close()
        }
    }

    @Test
    fun scavengeDoesNotTouchUnmarkedLegacyDirectoriesOrSymlinkCandidates() = withCodeDirectory { root ->
        val source = codeArchive(root, "source.apk", listOf("classes.dex" to "sample.A"))
        val storage = File(root, "snapshots").apply { mkdirs() }
        val legacy = File(storage, "impad-code-legacy").apply { mkdirs() }
        val legacyFile = File(legacy, "old.dex").apply { writeText("keep") }
        val external = File(root, "external").apply { mkdirs() }
        val externalFile = File(external, "keep.txt").apply { writeText("outside") }
        val link = File(storage, "impad-code-v1-${UUID.randomUUID()}")
        Files.createSymbolicLink(link.toPath(), external.toPath())

        StartupCodeSnapshot().capture("app", inputs(source), storage).close()

        assertTrue(legacyFile.isFile)
        assertTrue(Files.isSymbolicLink(link.toPath()))
        assertEquals("outside", externalFile.readText())
    }

    @Test
    fun symlinkedLifecycleLockIsRejectedWithoutTouchingItsTarget() = withCodeDirectory { root ->
        val source = codeArchive(root, "source.apk", listOf("classes.dex" to "sample.A"))
        val storage = File(root, "snapshots").apply { mkdirs() }
        val externalLock = File(root, "external.lock").apply { writeText("keep") }
        val lifecycleLock = File(storage, ".impad-code-lifecycle.lock")
        Files.createSymbolicLink(lifecycleLock.toPath(), externalLock.toPath())

        assertFailsWith<Exception> {
            StartupCodeSnapshot().capture("app", inputs(source), storage)
        }

        assertTrue(Files.isSymbolicLink(lifecycleLock.toPath()))
        assertEquals("keep", externalLock.readText())
        assertFalse(storage.listFiles().orEmpty().any { it.name.startsWith("impad-code-v1-") })
    }

    @Test
    fun concurrentCaptureDuringSnapshotPublicationCannotScavengeTheNewLease() = withCodeDirectory { root ->
        val source = codeArchive(root, "source.apk", listOf("classes.dex" to "sample.A"))
        val storage = File(root, "snapshots").apply { mkdirs() }
        val enteredCopy = CountDownLatch(1)
        val continueCopy = CountDownLatch(1)
        val firstCapture = AtomicReference<CapturedCodeSnapshot?>()
        val firstFailure = AtomicReference<Throwable?>()
        val worker = Thread {
            try {
                firstCapture.set(StartupCodeSnapshot(openSource = { file ->
                    enteredCopy.countDown()
                    check(continueCopy.await(10, TimeUnit.SECONDS))
                    file.inputStream()
                }).capture("app", inputs(source), storage))
            } catch (failure: Throwable) {
                firstFailure.set(failure)
            }
        }
        worker.start()
        assertTrue(enteredCopy.await(10, TimeUnit.SECONDS))
        val publishedDirectory = storage.listFiles().orEmpty().singleOrNull { it.name.startsWith("impad-code-v1-") }
        try {
            StartupCodeSnapshot().capture("app", inputs(source), storage).close()
            assertTrue(publishedDirectory?.isDirectory == true)
        } finally {
            continueCopy.countDown()
            worker.join(10_000)
        }
        firstFailure.get()?.let { throw AssertionError("first capture failed", it) }
        assertFalse(worker.isAlive)
        assertTrue(firstCapture.get()?.dexes?.single()?.file?.isFile == true)
        firstCapture.get()?.close()
        assertNoOwnedSnapshotDirectories(storage)
    }

    @Test
    fun unreadableMissingOrEmptyCodeSetIsRejected() = withCodeDirectory { root ->
        assertFailsWith<IllegalArgumentException> { capture(File(root, "missing.apk")) }
        val empty = File(root, "empty.apk")
        ZipOutputStream(empty.outputStream()).use { }
        assertFailsWith<IllegalArgumentException> { capture(empty) }
    }

    @Test
    fun inputReplacementWhileCopyingIsRejectedAndCopiesAreCleaned() = withCodeDirectory { root ->
        val source = codeArchive(root, "source.apk", listOf("classes.dex" to "sample.A"))
        val replacement = codeArchive(root, "replacement.apk", listOf("classes.dex" to "sample.B"))
        val storage = File(root, "snapshots").apply { mkdirs() }
        var replaced = false
        val reader = StartupCodeSnapshot(openSource = { file ->
            object : FilterInputStream(file.inputStream()) {
                override fun close() {
                    super.close()
                    if (!replaced) {
                        replaced = true
                        Files.move(replacement.toPath(), source.toPath(), java.nio.file.StandardCopyOption.REPLACE_EXISTING)
                    }
                }
            }
        })
        assertFailsWith<IllegalStateException> { reader.capture("app", inputs(source), storage) }
        assertTrue(replaced)
        assertTrue(source.isFile)
        assertNoOwnedSnapshotDirectories(storage)
    }

    @Test
    fun corruptDexIsRejectedAndCopiesAreCleaned() = withCodeDirectory { root ->
        val source = File(root, "corrupt.apk")
        ZipOutputStream(source.outputStream()).use { zip ->
            zip.putNextEntry(ZipEntry("classes.dex")); zip.write(byteArrayOf(1, 2, 3)); zip.closeEntry()
        }
        val storage = File(root, "snapshots").apply { mkdirs() }
        assertFailsWith<Exception> { StartupCodeSnapshot().capture("app", inputs(source), storage) }
        assertNoOwnedSnapshotDirectories(storage)
    }

    @Test
    fun cleanupFailureIsSuppressedWithoutReplacingCaptureFailure() = withCodeDirectory { root ->
        val source = codeArchive(root, "source.apk", listOf("classes.dex" to "sample.A"))
        val storage = File(root, "snapshots").apply { mkdirs() }
        val outside = File(root, "outside.txt").apply { writeText("keep") }
        val primary = IllegalStateException("primary capture failure")

        val thrown = assertFailsWith<IllegalStateException> {
            StartupCodeSnapshot(openSource = {
                val snapshotDirectory = storage.listFiles().orEmpty().single { it.name.startsWith("impad-code-v1-") }
                Files.createSymbolicLink(File(snapshotDirectory, "unexpected-link").toPath(), outside.toPath())
                throw primary
            }).capture("app", inputs(source), storage)
        }

        assertSame(primary, thrown)
        assertTrue(thrown.suppressed.any { it.message?.contains("symbolic link") == true })
        assertEquals("keep", outside.readText())
        assertTrue(storage.listFiles().orEmpty().any { it.name.startsWith("impad-code-v1-") })
    }

    private fun inputs(source: File) = listOf(CodeLoaderInput("app", null, CodeDelegation.PARENT_FIRST, listOf(source)))
    private fun capture(source: File) = StartupCodeSnapshot().capture("app", inputs(source))

    private fun assertNoOwnedSnapshotDirectories(storage: File) {
        assertFalse(storage.listFiles().orEmpty().any { it.name.startsWith("impad-code-v1-") })
    }

    private fun startSnapshotLeaseChild(source: File, storage: File): Process {
        val java = File(System.getProperty("java.home"), "bin/java").absolutePath
        val classpath = System.getProperty("java.class.path")
        return ProcessBuilder(java, "-cp", classpath, SnapshotLeaseChildProcess::class.java.name,
            source.absolutePath, storage.absolutePath).redirectErrorStream(true).start()
    }
}

object SnapshotLeaseChildProcess {
    @JvmStatic
    fun main(args: Array<String>) {
        val source = File(args[0])
        val storage = File(args[1])
        val snapshot = StartupCodeSnapshot().capture("app", listOf(
            CodeLoaderInput("app", null, CodeDelegation.PARENT_FIRST, listOf(source)),
        ), storage)
        val directory = snapshot.dexes.single().file.parentFile
        println("READY:${directory.name}")
        System.out.flush()
        System.`in`.read()
    }
}

internal fun withCodeDirectory(block: (File) -> Unit) {
    val directory = Files.createTempDirectory("impad-code-test-").toFile()
    try { block(directory) } finally { directory.deleteRecursively() }
}

internal fun codeArchive(root: File, name: String, entries: List<Pair<String, String>>): File {
    return codeArchiveWithClasses(root, name, entries.map { it.first to listOf(it.second) })
}

internal fun codeArchiveWithClasses(root: File, name: String, entries: List<Pair<String, List<String>>>): File {
    val output = File(root, name)
    ZipOutputStream(output.outputStream()).use { zip ->
        entries.forEach { (entry, classNames) ->
            val raw = Files.createTempFile(root.toPath(), "fixture", ".dex")
            try {
                val types = classNames.map { className -> ImmutableClassDef("L${className.replace('.', '/')};", 1,
                    "Ljava/lang/Object;", emptyList(), null, emptySet(), emptyList(), emptyList()) }
                DexPool.writeTo(raw.toString(), ImmutableDexFile(Opcodes.getDefault(), types))
                zip.putNextEntry(ZipEntry(entry)); Files.copy(raw, zip); zip.closeEntry()
            } finally { Files.deleteIfExists(raw) }
        }
    }
    return output
}
