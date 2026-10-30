package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.code.CodeContainerKind
import java.io.File
import java.io.FilterInputStream
import java.nio.file.Files
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNotEquals
import kotlin.test.assertTrue

class StartupCodeInputObserverTest {
    @Test
    fun identityUsesContentAndOrderNotFileLocationOrLoaderObjectAddress() = withInputs { root, a, b ->
        val copied = File(root, "copied.apk").also { a.copyTo(it) }
        fun observe(files: List<File>) = observer(files).observe(Loader())
        assertEquals(observe(listOf(a)).fingerprint, observe(listOf(copied)).fingerprint)
        assertNotEquals(observe(listOf(a, b)).fingerprint, observe(listOf(b, a)).fingerprint)
        assertEquals(CodeContainerKind.ZIP, observe(listOf(a)).containersByLoader.getValue("loader-0").single().kind)
    }

    @Test
    fun contentChangeInvalidatesObservationEvenIfLayoutPathsStaySame() = withInputs { _, a, _ ->
        val input = observer(listOf(a)).observe(Loader())
        assertTrue(input.inputsStillCurrent())
        a.appendBytes(byteArrayOf(1))
        assertFalse(input.inputsStillCurrent())
        assertNotEquals(input.fingerprint, observer(listOf(a)).observe(Loader()).fingerprint)
    }

    @Test
    fun layoutChangeDuringOrAfterHashingCannotProduceCurrentInput() = withInputs { _, a, b ->
        var paths = listOf(a)
        val reader = StartupLoaderLayoutReader { StartupLoaderDescription("dalvik.system.PathClassLoader", null, paths) }
        val input = StartupCodeInputObserver(reader).observe(Loader())
        paths = listOf(b)
        assertFalse(input.inputsStillCurrent())
        var reads = 0
        val changing = StartupLoaderLayoutReader {
            StartupLoaderDescription("dalvik.system.PathClassLoader", null, if (++reads == 1) listOf(a) else listOf(b))
        }
        assertFailsWith<StartupCodeChangedException> { StartupCodeInputObserver(changing).observe(Loader()) }
    }

    @Test
    fun replacementWhileHashingIsRejected() = withInputs { root, a, b ->
        val replacement = File(root, "replacement.apk").also { b.copyTo(it) }
        var replaced = false
        val instance = StartupCodeInputObserver(reader(listOf(a)), openSource = { file ->
            object : FilterInputStream(file.inputStream()) {
                override fun close() {
                    super.close()
                    if (!replaced) {
                        replaced = true
                        Files.move(replacement.toPath(), a.toPath(), java.nio.file.StandardCopyOption.REPLACE_EXISTING)
                    }
                }
            }
        })
        assertFailsWith<StartupCodeChangedException> { instance.observe(Loader()) }
    }

    @Test
    fun sameMetadataRewriteStillFailsTheContentRecheck() = withInputs { _, a, b ->
        val originalTime = Files.getLastModifiedTime(a.toPath())
        val originalSize = a.length()
        val input = observer(listOf(a)).observe(Loader())
        assertEquals(originalSize, b.length())
        a.writeBytes(b.readBytes())
        Files.setLastModifiedTime(a.toPath(), originalTime)
        assertEquals(originalSize, a.length())
        assertEquals(originalTime, Files.getLastModifiedTime(a.toPath()))
        assertFalse(input.inputsStillCurrent())
    }

    @Test
    fun inputRemovedAfterReadIsAChangeRatherThanAnUnsupportedInitialInput() = withInputs { _, a, _ ->
        val instance = StartupCodeInputObserver(reader(listOf(a)), openSource = { file ->
            object : FilterInputStream(file.inputStream()) {
                override fun close() { super.close(); Files.delete(file.toPath()) }
            }
        })
        assertFailsWith<StartupCodeChangedException> { instance.observe(Loader()) }
    }

    @Test
    fun unknownFileFormatIsExplicitlyUnsupported() = withInputs { root, _, _ ->
        val file = File(root, "encrypted.bin").apply { writeText("not a zip or standard dex") }
        assertFailsWith<UnsupportedStartupCodeException> { observer(listOf(file)).observe(Loader()) }
    }

    private fun observer(files: List<File>) = StartupCodeInputObserver(reader(files))
    private fun reader(files: List<File>) = StartupLoaderLayoutReader { StartupLoaderDescription("dalvik.system.PathClassLoader", null, files) }
    private fun withInputs(block: (File, File, File) -> Unit) {
        val root = Files.createTempDirectory("code-observer-test-").toFile()
        try {
            fun zip(name: String, value: Int) = File(root, name).apply {
                ZipOutputStream(outputStream()).use { out ->
                    out.putNextEntry(ZipEntry("asset")); out.write(byteArrayOf(value.toByte())); out.closeEntry()
                }
            }
            block(root, zip("a.apk", 1), zip("b.apk", 2))
        } finally { root.deleteRecursively() }
    }
    private class Loader : ClassLoader(null)
}
