package io.github.nku100.impad.compat.code

import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.security.MessageDigest
import java.util.Collections

enum class CodeDelegation { PARENT_FIRST, DELEGATE_LAST }

class CodeLoader(
    val id: String,
    val parentId: String?,
    val delegation: CodeDelegation,
    dexSha256: List<String>,
) {
    val dexSha256: List<String> = immutableList(dexSha256)
}

class CodeSnapshot(
    val readyLoaderId: String,
    loaders: List<CodeLoader>,
    val protocolVersion: Int = 1,
) {
    val loaders: List<CodeLoader> = immutableList(loaders.map {
        CodeLoader(it.id, it.parentId, it.delegation, it.dexSha256)
    })
    private val byId: Map<String, CodeLoader>

    init {
        require(protocolVersion == 1) { "Unsupported protocol version: $protocolVersion" }
        require(this.loaders.isNotEmpty()) { "Snapshot must contain loaders" }
        require(this.loaders.all { it.id.isNotBlank() }) { "Loader IDs must be nonblank" }
        require(this.loaders.map { it.id }.toSet().size == this.loaders.size) { "Loader IDs must be unique" }
        require(this.loaders.sumOf { it.dexSha256.size } > 0) { "Snapshot must contain at least one DEX" }
        require(this.loaders.flatMap { it.dexSha256 }.all { it.matches(SHA256_PATTERN) }) {
            "DEX hashes must be 64 hexadecimal characters"
        }
        byId = this.loaders.associateBy { it.id }
        require(readyLoaderId in byId) { "Ready loader does not exist" }
        this.loaders.forEach { loader ->
            require(loader.parentId == null || loader.parentId in byId) { "Parent loader does not exist: ${loader.parentId}" }
            val seen = mutableSetOf<String>()
            var current: CodeLoader? = loader
            while (current != null) {
                require(seen.add(current.id)) { "Loader parent cycle at ${current.id}" }
                current = current.parentId?.let(byId::get)
            }
        }
        val reachable = generateSequence(readyLoaderId) { byId.getValue(it).parentId }.toSet()
        require(reachable.size == this.loaders.size) { "Every loader must be in the ready loader ancestor chain" }
    }

    fun resolutionOrder(fromLoaderId: String = readyLoaderId): List<String> {
        require(fromLoaderId in byId) { "Unknown loader: $fromLoaderId" }
        val order = mutableListOf<String>()
        fun append(id: String?) {
            if (id == null) return
            val loader = byId.getValue(id)
            if (loader.delegation == CodeDelegation.PARENT_FIRST) append(loader.parentId)
            order += id
            if (loader.delegation == CodeDelegation.DELEGATE_LAST) append(loader.parentId)
        }
        append(fromLoaderId)
        return immutableList(order.distinct())
    }

    fun contentSha256(): String {
        val bytes = ByteArrayOutputStream()
        DataOutputStream(bytes).use { out ->
            out.writeInt(protocolVersion)
            writeString(out, readyLoaderId)
            val ordered = loaders.sortedBy { it.id }
            out.writeInt(ordered.size)
            ordered.forEach { loader ->
                writeString(out, loader.id)
                writeNullableString(out, loader.parentId)
                writeString(out, loader.delegation.name)
                out.writeInt(loader.dexSha256.size)
                loader.dexSha256.forEach { writeString(out, it.lowercase()) }
            }
        }
        return MessageDigest.getInstance("SHA-256").digest(bytes.toByteArray())
            .joinToString("") { "%02x".format(it) }
    }

    internal fun copySnapshot(): CodeSnapshot = CodeSnapshot(readyLoaderId, loaders, protocolVersion)

    companion object {
        private val SHA256_PATTERN = Regex("[0-9a-fA-F]{64}")
    }
}

data class DexLocation(val loaderId: String, val dexIndex: Int)

class EffectiveClassIndex(snapshot: CodeSnapshot, definitions: Map<DexLocation, Set<String>>) {
    val snapshot: CodeSnapshot = snapshot.copySnapshot()
    val definitions: Map<DexLocation, Set<String>> = Collections.unmodifiableMap(
        definitions.mapValues { (_, names) -> Collections.unmodifiableSet(names.toSet()) }.toMap(),
    )
    private val locationRanksByNamespace: Map<String, Map<DexLocation, Int>>
    private val locationsByClass: Map<String, List<DexLocation>>

    init {
        val expected = this.snapshot.loaders.flatMap { loader ->
            loader.dexSha256.indices.map { DexLocation(loader.id, it) }
        }.toSet()
        require(this.definitions.keys == expected) { "Definitions must cover exactly every DEX location" }

        val loadersById = this.snapshot.loaders.associateBy { it.id }
        locationRanksByNamespace = Collections.unmodifiableMap(
            this.snapshot.loaders.associate { namespace ->
                val orderedLocations = this.snapshot.resolutionOrder(namespace.id).flatMap { loaderId ->
                    val loader = loadersById.getValue(loaderId)
                    loader.dexSha256.indices.map { DexLocation(loaderId, it) }
                }
                namespace.id to Collections.unmodifiableMap(orderedLocations.withIndex().associate { (rank, location) ->
                    location to rank
                })
            },
        )

        val locationsByName = linkedMapOf<String, MutableList<DexLocation>>()
        this.definitions.forEach { (location, classNames) ->
            classNames.forEach { className -> locationsByName.getOrPut(className, ::mutableListOf).add(location) }
        }
        locationsByClass = Collections.unmodifiableMap(locationsByName.mapValues { (_, locations) ->
            Collections.unmodifiableList(locations.toList())
        })
    }

    fun ownerOf(className: String, fromLoaderId: String = snapshot.readyLoaderId): DexLocation? {
        val locationRanks = locationRanksByNamespace[fromLoaderId]
            ?: throw IllegalArgumentException("Unknown loader: $fromLoaderId")
        return locationsByClass[className]
            ?.mapNotNull { location -> locationRanks[location]?.let { rank -> rank to location } }
            ?.minByOrNull { it.first }
            ?.second
    }
}

private fun writeString(out: DataOutputStream, value: String) {
    val bytes = value.toByteArray(Charsets.UTF_8)
    out.writeInt(bytes.size)
    out.write(bytes)
}

private fun writeNullableString(out: DataOutputStream, value: String?) {
    out.writeBoolean(value != null)
    if (value != null) writeString(out, value)
}

private fun <T> immutableList(values: Collection<T>): List<T> =
    Collections.unmodifiableList(values.toList())
