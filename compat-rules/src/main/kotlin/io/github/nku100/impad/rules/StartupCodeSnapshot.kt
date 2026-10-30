package io.github.nku100.impad.rules

import com.android.tools.smali.dexlib2.Opcodes
import com.android.tools.smali.dexlib2.dexbacked.DexBackedDexFile
import io.github.nku100.impad.compat.code.CodeDelegation
import io.github.nku100.impad.compat.code.CodeArchiveManifest
import io.github.nku100.impad.compat.code.CodeContainerKind
import io.github.nku100.impad.compat.code.CodeLoader
import io.github.nku100.impad.compat.code.CodeSnapshot
import io.github.nku100.impad.compat.code.DexLocation
import io.github.nku100.impad.compat.code.EffectiveClassIndex
import java.io.File
import java.io.InputStream
import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.channels.FileLock
import java.nio.channels.OverlappingFileLockException
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.StandardOpenOption
import java.nio.file.attribute.BasicFileAttributes
import java.security.MessageDigest
import java.util.Collections
import java.util.UUID
import java.util.zip.CRC32
import java.util.zip.ZipEntry
import java.util.zip.ZipFile
import java.util.zip.ZipOutputStream

open class CodeInputChangedException(message: String) : IllegalStateException(message)

data class CodeLoaderInput(
    val id: String,
    val parentId: String?,
    val delegation: CodeDelegation,
    val archives: List<File>,
)

class CapturedCodeDex internal constructor(
    val location: DexLocation,
    val file: File,
    classNames: Set<String>,
) {
    val classNames: Set<String> = Collections.unmodifiableSet(classNames.toSet())
}

class CapturedCodeSnapshot internal constructor(
    val metadata: CodeSnapshot,
    dexes: List<CapturedCodeDex>,
    archiveManifests: List<CodeArchiveManifest>,
    private val sourcesStillCurrent: () -> Boolean,
    private val cleanup: () -> Unit,
) : AutoCloseable {
    val dexes: List<CapturedCodeDex> = Collections.unmodifiableList(dexes.toList())
    val archiveManifests: List<CodeArchiveManifest> = Collections.unmodifiableList(archiveManifests.toList())
    val definitions = EffectiveClassIndex(metadata, dexes.associate { it.location to it.classNames })
    private var closed = false

    fun ensureOpen() = check(!closed) { "Code snapshot is closed" }

    fun inputsStillCurrent(): Boolean = !closed && sourcesStillCurrent()

    @Synchronized
    override fun close() {
        if (closed) return
        closed = true
        cleanup()
    }
}

/** Captures observed startup files, not the ART origin of already resident methods. */
class StartupCodeSnapshot(
    private val openSource: (File) -> InputStream = File::inputStream,
) {
    fun capture(
        readyLoaderId: String,
        loaders: List<CodeLoaderInput>,
        storageDirectory: File? = null,
    ): CapturedCodeSnapshot {
        val inputs = loaders.map { it.copy(archives = it.archives.toList()) }
        // Validate topology before creating or reading any files; placeholder hashes are not persisted.
        CodeSnapshot(readyLoaderId, inputs.map { CodeLoader(it.id, it.parentId, it.delegation, listOf("0".repeat(64))) })
        val stamps = inputs.flatMap { it.archives }.distinct().associateWith(::stamp)
        val lease = createSnapshotLease(snapshotStorageRoot(storageDirectory))
        val directory = lease.directory
        try {
            val copied = LinkedHashMap<File, Pair<File, String>>()
            stamps.forEach { (source, before) ->
                val copy = File(directory, "source-${copied.size}.apk")
                val digest = MessageDigest.getInstance("SHA-256")
                openSource(source).buffered().use { input -> copy.outputStream().buffered().use { output ->
                    transferWithHash(input, output, digest)
                } }
                if ((try { stamp(source) } catch (_: Exception) { null }) != before) throw CodeInputChangedException("Code input changed while being copied: $source")
                copied[source] = copy to digest.digest().hex()
            }
            val dexes = mutableListOf<CapturedCodeDex>()
            val manifests = mutableListOf<CodeArchiveManifest>()
            val models = inputs.map { loader ->
                val hashes = mutableListOf<String>()
                fun captureDex(input: InputStream, size: Long, crc: Long) {
                    val location = DexLocation(loader.id, hashes.size)
                    val file = File(directory, "dex-${dexes.size}.apk")
                    val digest = MessageDigest.getInstance("SHA-256")
                    ZipOutputStream(file.outputStream().buffered()).use { output ->
                        val entry = ZipEntry("classes.dex").apply {
                            method = ZipEntry.STORED
                            this.size = size
                            compressedSize = size
                            this.crc = crc
                            // Avoid compression work and keep the stored DEX data aligned for native parsing.
                            extra = byteArrayOf(-1, -1, 3, 0, 0, 0, 0)
                        }
                        output.putNextEntry(entry)
                        transferWithHash(input, output, digest)
                        output.closeEntry()
                    }
                    hashes += digest.digest().hex()
                    val names = ZipFile(file).use { archive ->
                        archive.getInputStream(archive.getEntry("classes.dex")).buffered().use { stream ->
                            DexBackedDexFile.fromInputStream(Opcodes.getDefault(), stream).classes
                                .map { definition -> definition.type.removePrefix("L").removeSuffix(";").replace('/', '.') }.toSet()
                        }
                    }
                    dexes += CapturedCodeDex(location, file, names)
                }
                loader.archives.forEachIndexed { containerIndex, source ->
                    val firstDexIndex = hashes.size
                    val copy = copied.getValue(source).first
                    val isRawDex = copy.inputStream().use { stream ->
                        val magic = ByteArray(4)
                        stream.read(magic) == 4 && magic.contentEquals(byteArrayOf(100, 101, 120, 10))
                    }
                    if (isRawDex) {
                        val crc = CRC32()
                        copy.inputStream().buffered().use { input ->
                            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                            while (true) {
                                val count = input.read(buffer)
                                if (count < 0) break
                                crc.update(buffer, 0, count)
                            }
                        }
                        copy.inputStream().buffered().use { captureDex(it, copy.length(), crc.value) }
                    }
                    else ZipFile(copy).use { zip ->
                        val entries = zip.entries().asSequence()
                            .filter { !it.isDirectory && DEX_ENTRY.matches(it.name) }.toList()
                            .sortedBy { dexOrdinal(it.name) }
                        require(entries.map { it.name }.distinct().size == entries.size) { "Duplicate DEX archive entries" }
                        require(entries.withIndex().all { (index, entry) -> dexOrdinal(entry.name) == index + 1L }) {
                            "Unsupported noncontiguous DEX archive layout"
                        }
                        entries.forEach { entry -> zip.getInputStream(entry).buffered().use { captureDex(it, entry.size, entry.crc) } }
                    }
                    manifests += CodeArchiveManifest(loader.id, containerIndex,
                        if (isRawDex) CodeContainerKind.DEX else CodeContainerKind.ZIP,
                        copied.getValue(source).second, hashes.subList(firstDexIndex, hashes.size))
                }
                CodeLoader(loader.id, loader.parentId, loader.delegation, hashes)
            }
            fun checkSources() {
                stamps.forEach { (source, before) ->
                    if ((try { stamp(source) } catch (_: Exception) { null }) != before) throw CodeInputChangedException("Code input changed during snapshot capture: $source")
                    val digest = MessageDigest.getInstance("SHA-256")
                    openSource(source).buffered().use { input ->
                        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                        while (true) {
                            val count = input.read(buffer)
                            if (count < 0) break
                            digest.update(buffer, 0, count)
                        }
                    }
                    if (digest.digest().hex() != copied.getValue(source).second || (try { stamp(source) } catch (_: Exception) { null }) != before)
                        throw CodeInputChangedException("Code input content changed during snapshot capture: $source")
                }
            }
            checkSources()
            copied.values.forEach { (file, _) -> Files.delete(file.toPath()) }
            return CapturedCodeSnapshot(CodeSnapshot(readyLoaderId, models), dexes, manifests, {
                try { checkSources(); true } catch (_: Exception) { false }
            }) { cleanupSnapshotLease(lease) }
        } catch (failure: Throwable) {
            try { cleanupSnapshotLease(lease) } catch (cleanup: Throwable) { failure.addSuppressed(cleanup) }
            throw failure
        }
    }

    private fun stamp(file: File): SourceStamp {
        require(file.isFile && file.canRead()) { "Code input is not a readable file: $file" }
        val canonical = file.canonicalFile
        val attributes = Files.readAttributes(canonical.toPath(), BasicFileAttributes::class.java)
        return SourceStamp(canonical, attributes.fileKey(), attributes.size(), attributes.lastModifiedTime())
    }
}

private data class SourceStamp(val canonicalFile: File, val fileKey: Any?, val size: Long, val modified: Any)
private data class SnapshotLease(
    val root: java.nio.file.Path,
    val directory: File,
    val channel: FileChannel,
    val lock: FileLock,
)

private val snapshotLifecycleMonitor = Any()
private const val SNAPSHOT_DIRECTORY_PREFIX = "impad-code-v1-"
private const val SNAPSHOT_DIRECTORY_NAME = "impad-code-v1-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
private const val SNAPSHOT_OWNER_FILE = ".impad-code-owner"
private const val SNAPSHOT_LEASE_FILE = ".lease"
private const val SNAPSHOT_LIFECYCLE_LOCK_FILE = ".impad-code-lifecycle.lock"
private val SNAPSHOT_OWNER_MARKER = "impad-code-snapshot-v1\n".toByteArray(Charsets.US_ASCII)
private val DEX_ENTRY = Regex("classes(?:[2-9]|[1-9][0-9]+)?\\.dex")
private fun dexOrdinal(name: String): Long = if (name == "classes.dex") 1L else
    requireNotNull(name.removePrefix("classes").removeSuffix(".dex").toLongOrNull()) { "Invalid DEX ordinal" }

private fun transferWithHash(input: InputStream, output: java.io.OutputStream, digest: MessageDigest) {
    val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
    while (true) {
        val count = input.read(buffer)
        if (count < 0) break
        digest.update(buffer, 0, count)
        output.write(buffer, 0, count)
    }
}

private fun ByteArray.hex(): String = joinToString("") { "%02x".format(it) }

private fun snapshotStorageRoot(storageDirectory: File?): java.nio.file.Path {
    val root = (storageDirectory ?: File(System.getProperty("java.io.tmpdir")))
        .toPath().toAbsolutePath().normalize()
    require(!Files.isSymbolicLink(root) && Files.isDirectory(root, LinkOption.NOFOLLOW_LINKS)) {
        "Snapshot storage root must be a real directory: $root"
    }
    return root
}

private fun <T> withSnapshotStorageLock(root: java.nio.file.Path, action: () -> T): T =
    synchronized(snapshotLifecycleMonitor) {
        require(!Files.isSymbolicLink(root) && Files.isDirectory(root, LinkOption.NOFOLLOW_LINKS)) {
            "Snapshot storage root must be a real directory: $root"
        }
        val path = root.resolve(SNAPSHOT_LIFECYCLE_LOCK_FILE)
        if (Files.exists(path, LinkOption.NOFOLLOW_LINKS)) {
            require(Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS)) {
                "Unsafe snapshot lifecycle lock: $path"
            }
        }
        val channel = FileChannel.open(path, StandardOpenOption.CREATE, StandardOpenOption.WRITE,
            LinkOption.NOFOLLOW_LINKS)
        try {
            require(Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS)) { "Unsafe snapshot lifecycle lock: $path" }
            channel.lock().use { action() }
        } finally {
            channel.close()
        }
    }

private fun createSnapshotLease(root: java.nio.file.Path): SnapshotLease = withSnapshotStorageLock(root) {
    scavengeAbandonedSnapshots(root)
    val directory = root.resolve(SNAPSHOT_DIRECTORY_PREFIX + UUID.randomUUID()).normalize()
    check(directory.parent == root) { "Snapshot directory escaped its storage root" }
    Files.createDirectory(directory)
    var channel: FileChannel? = null
    var lock: FileLock? = null
    try {
        val leasePath = directory.resolve(SNAPSHOT_LEASE_FILE)
        channel = FileChannel.open(leasePath, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE,
            LinkOption.NOFOLLOW_LINKS)
        lock = channel.lock()
        val marker = directory.resolve(SNAPSHOT_OWNER_FILE)
        FileChannel.open(marker, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE,
            LinkOption.NOFOLLOW_LINKS).use { markerChannel ->
            markerChannel.write(ByteBuffer.wrap(SNAPSHOT_OWNER_MARKER))
            markerChannel.force(true)
        }
        SnapshotLease(root, directory.toFile(), channel, lock)
    } catch (failure: Throwable) {
        try { lock?.release() } catch (cleanup: Throwable) { failure.addSuppressed(cleanup) }
        try { channel?.close() } catch (cleanup: Throwable) { failure.addSuppressed(cleanup) }
        try { deleteSnapshotTree(root, directory) } catch (cleanup: Throwable) { failure.addSuppressed(cleanup) }
        throw failure
    }
}

private fun cleanupSnapshotLease(lease: SnapshotLease) {
    var failure: Throwable? = null
    var leaseReleased = false
    var channelClosed = false
    fun recordFailure(cleanup: Throwable) {
        val primary = failure
        if (primary == null) failure = cleanup else if (primary !== cleanup) primary.addSuppressed(cleanup)
    }
    fun releaseLease() {
        if (!leaseReleased) {
            try {
                lease.lock.release()
                leaseReleased = true
            } catch (cleanup: Throwable) {
                recordFailure(cleanup)
            }
        }
    }
    fun closeChannel() {
        if (!channelClosed) {
            try {
                lease.channel.close()
                channelClosed = true
            } catch (cleanup: Throwable) {
                recordFailure(cleanup)
            }
        }
    }

    try {
        withSnapshotStorageLock(lease.root) {
            releaseLease()
            closeChannel()
            if (failure == null) {
                try { deleteSnapshotTree(lease.root, lease.directory.toPath()) }
                catch (cleanup: Throwable) { recordFailure(cleanup) }
            }
        }
    } catch (cleanup: Throwable) {
        recordFailure(cleanup)
    } finally {
        releaseLease()
        closeChannel()
    }
    failure?.let { throw it }
}

private fun scavengeAbandonedSnapshots(root: java.nio.file.Path) {
    Files.newDirectoryStream(root).use { entries ->
        entries.forEach { directory ->
            if (directory.fileName.toString().matches(Regex(SNAPSHOT_DIRECTORY_NAME)) && isOwnedSnapshotDirectory(root, directory)) {
                val leasePath = directory.resolve(SNAPSHOT_LEASE_FILE)
                var channel: FileChannel? = null
                var lock: FileLock? = null
                try {
                    channel = FileChannel.open(leasePath, StandardOpenOption.WRITE, LinkOption.NOFOLLOW_LINKS)
                    lock = try {
                        channel.tryLock()
                    } catch (_: OverlappingFileLockException) {
                        null
                    }
                    if (lock != null) deleteSnapshotTree(root, directory)
                } catch (_: java.io.IOException) {
                    // An unreadable or concurrently replaced candidate is left untouched.
                } finally {
                    try { lock?.release() } finally { channel?.close() }
                }
            }
        }
    }
}

private fun isOwnedSnapshotDirectory(root: java.nio.file.Path, directory: java.nio.file.Path): Boolean {
    if (directory.parent != root || Files.isSymbolicLink(directory)
        || !Files.isDirectory(directory, LinkOption.NOFOLLOW_LINKS)) return false
    val marker = directory.resolve(SNAPSHOT_OWNER_FILE)
    val lease = directory.resolve(SNAPSHOT_LEASE_FILE)
    if (Files.isSymbolicLink(marker) || Files.isSymbolicLink(lease)
        || !Files.isRegularFile(marker, LinkOption.NOFOLLOW_LINKS)
        || !Files.isRegularFile(lease, LinkOption.NOFOLLOW_LINKS)) return false
    if (!Files.readAllBytes(marker).contentEquals(SNAPSHOT_OWNER_MARKER)) return false
    return !containsSymlink(directory)
}

private fun containsSymlink(directory: java.nio.file.Path): Boolean = Files.walk(directory).use { paths ->
    paths.anyMatch { Files.isSymbolicLink(it) }
}

private fun deleteSnapshotTree(root: java.nio.file.Path, directory: java.nio.file.Path) {
    val safeRoot = root.toAbsolutePath().normalize()
    val safeDirectory = directory.toAbsolutePath().normalize()
    check(safeDirectory.parent == safeRoot && safeDirectory.fileName.toString().matches(Regex(SNAPSHOT_DIRECTORY_NAME))) {
        "Refusing to remove a snapshot outside its owned storage directory: $directory"
    }
    if (!Files.exists(safeDirectory, LinkOption.NOFOLLOW_LINKS)) return
    check(!Files.isSymbolicLink(safeDirectory) && Files.isDirectory(safeDirectory, LinkOption.NOFOLLOW_LINKS)) {
        "Refusing to remove an unsafe snapshot directory: $safeDirectory"
    }
    val paths = Files.walk(safeDirectory).use { stream -> stream.iterator().asSequence().toList() }
    check(paths.none(Files::isSymbolicLink)) { "Refusing to remove a snapshot containing a symbolic link" }
    paths.sortedByDescending { it.nameCount }.forEach { path ->
        check(path == safeDirectory || path.startsWith(safeDirectory)) { "Snapshot child escaped its directory" }
        Files.deleteIfExists(path)
    }
}
