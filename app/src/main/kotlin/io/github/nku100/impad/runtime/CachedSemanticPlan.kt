package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.code.CodeArchiveManifest
import io.github.nku100.impad.compat.code.CodeContainerKind
import io.github.nku100.impad.compat.code.CodeDelegation
import io.github.nku100.impad.compat.code.CodeLoader
import io.github.nku100.impad.compat.code.CodeSnapshot
import io.github.nku100.impad.compat.rules.CompletePlan
import io.github.nku100.impad.rules.SemanticRuleCatalog
import java.util.Collections
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

/** Reuses a previously parsed manifest only for byte-identical observed containers. */
class CachedSemanticPlan private constructor(
    val inputFingerprint: String,
    val snapshot: CodeSnapshot,
    archives: List<CodeArchiveManifest>,
    val plan: CompletePlan,
) {
    val archives: List<CodeArchiveManifest> = Collections.unmodifiableList(archives.toList())

    fun encode(): String = Json.encodeToString(CachedPlanDocument(
        schemaVersion = 1, inputFingerprint = inputFingerprint,
        readyLoaderId = snapshot.readyLoaderId,
        loaders = snapshot.loaders.map { CachedLoader(it.id, it.parentId, it.delegation.name, it.dexSha256) },
        archives = archives.map { CachedArchive(it.loaderId, it.containerIndex, it.kind.name, it.containerSha256, it.dexSha256) },
        plan = plan,
    ))

    companion object {
        fun create(observed: ObservedCodeInputs, snapshot: CodeSnapshot, archives: List<CodeArchiveManifest>,
            plan: CompletePlan, catalog: SemanticRuleCatalog): CachedSemanticPlan {
            validateManifest(observed, snapshot, archives)
            validateContract(plan, snapshot, catalog)
            return CachedSemanticPlan(observed.fingerprint, snapshot, archives, plan)
        }

        fun decode(payload: String, observed: ObservedCodeInputs, catalog: SemanticRuleCatalog): CachedSemanticPlan? = try {
            val document = Json.decodeFromString<CachedPlanDocument>(payload)
            require(document.schemaVersion == 1 && document.inputFingerprint == observed.fingerprint)
            val snapshot = CodeSnapshot(document.readyLoaderId, document.loaders.map {
                CodeLoader(it.id, it.parentId, CodeDelegation.valueOf(it.delegation), it.dexSha256)
            })
            val archives = document.archives.map {
                CodeArchiveManifest(it.loaderId, it.containerIndex, CodeContainerKind.valueOf(it.kind), it.containerSha256, it.dexSha256)
            }
            create(observed, snapshot, archives, document.plan, catalog)
        } catch (_: IllegalArgumentException) { null }

        fun validateContract(plan: CompletePlan, snapshot: CodeSnapshot, catalog: SemanticRuleCatalog) {
            require(plan.snapshotSha256.equals(snapshot.contentSha256(), ignoreCase = true)) { "Plan belongs to another code snapshot" }
            require(plan.contractId == catalog.contractId && plan.contractRevision == catalog.contractRevision)
            val strategy = catalog.strategy(plan.strategyId)
            require(plan.strategyRevision == strategy.revision && plan.hooks.map { it.role } == strategy.roles.map { it.role })
            val loaders = snapshot.loaders.associateBy { it.id }
            plan.hooks.zip(strategy.roles).forEach { (hook, role) ->
                require(hook.phase == role.phase && hook.configuration == role.configuration)
                val hash = loaders[hook.source.loaderId]?.dexSha256?.getOrNull(hook.source.dexIndex)
                require(hash != null && hash.equals(hook.source.dexSha256, ignoreCase = true)) { "Hook source is outside the cached snapshot" }
            }
        }

        private fun validateManifest(observed: ObservedCodeInputs, snapshot: CodeSnapshot, archives: List<CodeArchiveManifest>) {
            val layout = observed.layout
            require(snapshot.readyLoaderId == layout.readyLoaderId && snapshot.loaders.size == layout.loaders.size)
            require(archives.map { it.loaderId to it.containerIndex }.distinct().size == archives.size) { "Duplicate container position" }
            val expectedPositions = layout.loaders.flatMap { loader -> loader.archives.indices.map { loader.id to it } }.toSet()
            require(archives.map { it.loaderId to it.containerIndex }.toSet() == expectedPositions) { "Incomplete container manifest" }
            val byId = snapshot.loaders.associateBy { it.id }
            layout.loaders.forEach { loader ->
                val model = requireNotNull(byId[loader.id])
                require(model.parentId == loader.parentId && model.delegation == loader.delegation)
                val entries = archives.filter { it.loaderId == loader.id }.sortedBy { it.containerIndex }
                val current = observed.containersByLoader.getValue(loader.id)
                require(entries.size == current.size)
                entries.zip(current).forEach { (entry, source) ->
                    require(entry.kind == source.kind && entry.containerSha256.equals(source.sha256, ignoreCase = true)) { "Captured container differs from observed bytes" }
                }
                require(entries.flatMap { it.dexSha256 }.map(String::lowercase) == model.dexSha256.map(String::lowercase)) { "DEX manifest does not reconstruct the snapshot" }
            }
        }
    }
}

@Serializable
private data class CachedPlanDocument(
    val schemaVersion: Int,
    val inputFingerprint: String,
    val readyLoaderId: String,
    val loaders: List<CachedLoader>,
    val archives: List<CachedArchive>,
    val plan: CompletePlan,
)
@Serializable
private data class CachedLoader(val id: String, val parentId: String?, val delegation: String, val dexSha256: List<String>)
@Serializable
private data class CachedArchive(val loaderId: String, val containerIndex: Int, val kind: String, val containerSha256: String, val dexSha256: List<String>)
