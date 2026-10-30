package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.code.CodeContainerKind
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.io.File
import java.io.InputStream
import java.nio.file.Files
import java.nio.file.attribute.BasicFileAttributes
import java.security.MessageDigest
import java.util.Collections

class StartupCodeChangedException(message: String) : io.github.nku100.impad.rules.CodeInputChangedException(message)
class UnsupportedStartupCodeException(message: String) : IllegalArgumentException(message)

class ObservedCodeContainer internal constructor(val file: File, val sha256: String, val kind: CodeContainerKind)

class ObservedCodeInputs internal constructor(
    val layout: StartupLoaderLayout,
    containersByLoader: Map<String, List<ObservedCodeContainer>>,
    private val stillCurrent: () -> Boolean,
) {
    val containersByLoader: Map<String, List<ObservedCodeContainer>> = Collections.unmodifiableMap(containersByLoader.mapValues {
        Collections.unmodifiableList(it.value.toList())
    })
    val fingerprint: String = containerFingerprint(layout, this.containersByLoader)
    fun inputsStillCurrent(): Boolean = try { stillCurrent() } catch (_: Exception) { false }
}

class StartupCodeInputObserver(
    private val reader: StartupLoaderLayoutReader = StartupLoaderLayoutReader(),
    private val openSource: (File) -> InputStream = File::inputStream,
) {
    fun observe(readyLoader: ClassLoader): ObservedCodeInputs {
        val layout = reader.read(readyLoader)
        val sources = layout.loaders.flatMap { it.archives }.distinct().associateWith(::readStable)
        if (!layout.sameAs(reader.read(readyLoader))) throw StartupCodeChangedException("Startup loader layout changed while hashing input")
        val rows = layout.loaders.associate { loader -> loader.id to loader.archives.map { file ->
            val source = sources.getValue(file)
            ObservedCodeContainer(file, source.sha256, source.kind)
        } }
        return ObservedCodeInputs(layout, rows) {
            layout.sameAs(reader.read(readyLoader)) && sources.all { (file, expected) ->
                val current = readStable(file)
                current.stamp == expected.stamp && current.sha256 == expected.sha256 && current.kind == expected.kind
            }
        }
    }

    private fun readStable(file: File): StableCodeFile {
        val before = stamp(file)
        val digest = MessageDigest.getInstance("SHA-256")
        val prefix = ByteArrayOutputStream(4)
        openSource(file).buffered().use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
                if (prefix.size() < 4) prefix.write(buffer, 0, minOf(count, 4 - prefix.size()))
            }
        }
        if ((try { stamp(file) } catch (_: Exception) { null }) != before) throw StartupCodeChangedException("Code input changed while hashing: $file")
        val magic = prefix.toByteArray()
        val kind = when {
            magic.contentEquals(byteArrayOf(100, 101, 120, 10)) -> CodeContainerKind.DEX
            magic.size >= 2 && magic[0] == 80.toByte() && magic[1] == 75.toByte() -> CodeContainerKind.ZIP
            else -> throw UnsupportedStartupCodeException("Code input is not standard DEX or ZIP: $file")
        }
        return StableCodeFile(digest.digest().hexDigest(), kind, before)
    }

    private fun stamp(file: File): CodeFileStamp {
        require(file.isFile && file.canRead()) { "Code input is not readable: $file" }
        val canonical = file.canonicalFile
        val attributes = Files.readAttributes(canonical.toPath(), BasicFileAttributes::class.java)
        return CodeFileStamp(canonical, attributes.fileKey(), attributes.size(), attributes.lastModifiedTime())
    }
}

private data class CodeFileStamp(val canonical: File, val key: Any?, val size: Long, val modified: Any)
private data class StableCodeFile(val sha256: String, val kind: CodeContainerKind, val stamp: CodeFileStamp)

private fun containerFingerprint(layout: StartupLoaderLayout, containers: Map<String, List<ObservedCodeContainer>>): String {
    val bytes = ByteArrayOutputStream()
    DataOutputStream(bytes).use { out ->
        out.containerString("STARTUP_FILE_CONTAINERS_V1")
        out.containerString(layout.readyLoaderId)
        out.writeInt(layout.loaders.size)
        layout.loaders.sortedBy { it.id }.forEach { loader ->
            out.containerString(loader.id)
            out.writeBoolean(loader.parentId != null)
            loader.parentId?.let(out::containerString)
            out.containerString(loader.delegation.name)
            val files = containers.getValue(loader.id)
            out.writeInt(files.size)
            files.forEach { source -> out.containerString(source.kind.name); out.containerString(source.sha256.lowercase()) }
        }
    }
    return MessageDigest.getInstance("SHA-256").digest(bytes.toByteArray()).hexDigest()
}

private fun DataOutputStream.containerString(value: String) {
    val bytes = value.toByteArray(Charsets.UTF_8)
    writeInt(bytes.size); write(bytes)
}
private fun ByteArray.hexDigest(): String = joinToString("") { "%02x".format(it) }
