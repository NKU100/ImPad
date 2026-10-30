package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.code.CodeDelegation
import io.github.nku100.impad.rules.CapturedCodeSnapshot
import io.github.nku100.impad.rules.CodeLoaderInput
import io.github.nku100.impad.rules.StartupCodeSnapshot
import java.io.File
import java.lang.reflect.Field
import java.util.Collections
import java.util.IdentityHashMap

data class StartupLoaderDescription(val className: String, val parent: ClassLoader?, val dexFiles: List<File>)

class StartupLoaderLayout internal constructor(
    val readyLoaderId: String,
    loaders: List<CodeLoaderInput>,
    bindings: Map<String, ClassLoader>,
) {
    val loaders: List<CodeLoaderInput> = Collections.unmodifiableList(loaders.map {
        it.copy(archives = Collections.unmodifiableList(it.archives.toList()))
    })
    val bindings: Map<String, ClassLoader> = Collections.unmodifiableMap(bindings.toMap())

    fun sameAs(other: StartupLoaderLayout): Boolean = readyLoaderId == other.readyLoaderId &&
        loaders == other.loaders && bindings.keys == other.bindings.keys &&
        bindings.all { (id, loader) -> loader === other.bindings[id] }
}

class StartupLoaderLayoutReader(
    private val describe: (ClassLoader) -> StartupLoaderDescription = ::describePlatformLoader,
) {
    fun read(readyLoader: ClassLoader): StartupLoaderLayout {
        val nodes = mutableListOf<Pair<ClassLoader, StartupLoaderDescription>>()
        val seen = Collections.newSetFromMap(IdentityHashMap<ClassLoader, Boolean>())
        var current: ClassLoader? = readyLoader
        while (current != null) {
            require(seen.add(current)) { "Class loader parent cycle" }
            val node = describe(current)
            if (node.className in BOOT_LOADERS) break
            require(node.className in FILE_LOADERS) { "Unsupported startup class loader: ${node.className}" }
            require(node.dexFiles.all { it.path.isNotBlank() }) { "Startup loader exposed an unnamed code input" }
            nodes += current to node.copy(dexFiles = node.dexFiles.map(File::getCanonicalFile))
            current = node.parent
        }
        require(nodes.isNotEmpty() && nodes.any { it.second.dexFiles.isNotEmpty() }) { "Ready loader exposed no application DEX inputs" }
        val loaders = nodes.mapIndexed { index, (_, node) ->
            CodeLoaderInput(
                id = "loader-$index",
                parentId = if (index + 1 < nodes.size) "loader-${index + 1}" else null,
                delegation = if (node.className == "dalvik.system.DelegateLastClassLoader") CodeDelegation.DELEGATE_LAST
                    else CodeDelegation.PARENT_FIRST,
                archives = node.dexFiles,
            )
        }
        return StartupLoaderLayout("loader-0", loaders, nodes.mapIndexed { index, pair -> "loader-$index" to pair.first }.toMap())
    }
}

/** Relates an observed file snapshot to its loader objects, without claiming ART method provenance. */
class StartupRuntimeSnapshot private constructor(
    val layout: StartupLoaderLayout,
    val snapshot: CapturedCodeSnapshot,
    private val readyLoader: ClassLoader,
    private val reader: StartupLoaderLayoutReader,
) : AutoCloseable {
    fun inputsStillCurrent(): Boolean = try {
        snapshot.ensureOpen()
        layout.sameAs(reader.read(readyLoader)) && snapshot.inputsStillCurrent()
    } catch (_: Exception) { false }

    fun requireDeclaringLoader(className: String, actualLoader: ClassLoader?) {
        snapshot.ensureOpen()
        val owner = requireNotNull(snapshot.definitions.ownerOf(className)) { "Class is absent from the startup snapshot: $className" }
        require(layout.bindings.getValue(owner.loaderId) === actualLoader) { "Declaring loader differs from the startup snapshot: $className" }
    }

    override fun close() = snapshot.close()

    companion object {
        fun capture(
            readyLoader: ClassLoader,
            storageDirectory: File,
            reader: StartupLoaderLayoutReader = StartupLoaderLayoutReader(),
        ): StartupRuntimeSnapshot {
            val layout = reader.read(readyLoader)
            val snapshot = StartupCodeSnapshot().capture(layout.readyLoaderId, layout.loaders, storageDirectory)
            try {
                check(layout.sameAs(reader.read(readyLoader))) { "Startup loader layout changed during capture" }
                return StartupRuntimeSnapshot(layout, snapshot, readyLoader, reader)
            } catch (failure: Throwable) {
                try { snapshot.close() } catch (cleanup: Throwable) { failure.addSuppressed(cleanup) }
                throw failure
            }
        }
    }
}

private val BOOT_LOADERS = setOf("java.lang.BootClassLoader", "dalvik.system.BootClassLoader")
private val FILE_LOADERS = setOf("dalvik.system.PathClassLoader", "dalvik.system.DexClassLoader", "dalvik.system.DelegateLastClassLoader")

private fun describePlatformLoader(loader: ClassLoader): StartupLoaderDescription {
    val name = loader.javaClass.name
    if (name in BOOT_LOADERS) return StartupLoaderDescription(name, null, emptyList())
    require(name in FILE_LOADERS) { "Unsupported startup class loader: $name" }
    val pathList = requireNotNull(loaderField(loader.javaClass, "pathList").get(loader)) { "Loader has no path list" }
    val elements = loaderField(pathList.javaClass, "dexElements").get(pathList) as? Array<*>
        ?: error("Loader has no ordered DEX elements")
    val files = elements.mapNotNull { element ->
        requireNotNull(element) { "Null DEX element" }
        val dex = loaderField(element.javaClass, "dexFile").get(element) ?: return@mapNotNull null
        val path = dex.javaClass.getMethod("getName").invoke(dex) as? String
        require(!path.isNullOrBlank()) { "In-memory or unnamed DEX inputs are unsupported" }
        File(path)
    }
    return StartupLoaderDescription(name, loader.parent, files)
}

private fun loaderField(type: Class<*>, name: String): Field {
    var current: Class<*>? = type
    while (current != null) {
        try { return current.getDeclaredField(name).apply { isAccessible = true } }
        catch (_: NoSuchFieldException) { current = current.superclass }
    }
    error("Startup loader field is unavailable: $name")
}
