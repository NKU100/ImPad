package io.github.nku100.impad.rules

import io.github.nku100.impad.compat.rules.TargetCodeSource
import java.io.File

/** Queries only the class definitions selected by an observed startup loader layout. */
object EffectiveCodeRuleSession {
    fun open(snapshot: CapturedCodeSnapshot): RuleSession = open(snapshot, DexKitSessionBackend::open)

    internal fun open(snapshot: CapturedCodeSnapshot, backendFactory: (File) -> RuleSessionBackend): RuleSession {
        snapshot.ensureOpen()
        val backends = mutableListOf<RuleSessionBackend>()
        try {
            snapshot.dexes.forEach { backends += backendFactory(it.file) }
            return EffectiveSession(EffectiveSessionState(snapshot, backends.toList()), snapshot.metadata.readyLoaderId, ownsState = true)
        } catch (failure: Throwable) {
            backends.asReversed().forEach {
                try { it.close() } catch (cleanup: Throwable) { failure.addSuppressed(cleanup) }
            }
            throw failure
        }
    }
}

private class EffectiveSessionState(val snapshot: CapturedCodeSnapshot, val backends: List<RuleSessionBackend>) {
    val identity = Any()
    val snapshotSha256 = snapshot.metadata.contentSha256()
    var closed = false
        private set

    fun ensureOpen() {
        check(!closed) { "Rule session is closed" }
        snapshot.ensureOpen()
    }

    fun close() {
        if (closed) return
        closed = true
        var failure: Throwable? = null
        backends.asReversed().forEach { backend ->
            try { backend.close() } catch (error: Throwable) {
                if (failure == null) failure = error else failure.addSuppressed(error)
            }
        }
        failure?.let { throw it }
    }
}

private class EffectiveSession(
    private val state: EffectiveSessionState,
    private val namespaceId: String,
    private val ownsState: Boolean,
) : RuleSession {
    private val snapshot get() = state.snapshot
    private var closed = false

    private fun ensureOpen() {
        check(!closed) { "Rule session view is closed" }
        state.ensureOpen()
    }

    private fun isVisible(className: String, index: Int): Boolean =
        snapshot.definitions.ownerOf(className, namespaceId) == snapshot.dexes[index].location

    override fun findMethods(query: MethodQuery): List<RuleMethod> {
        ensureOpen()
        return state.backends.flatMapIndexed { index, backend ->
            backend.findMethods(query).filter { isVisible(it.className, index) }
                .map { method -> method.copy(sourceDexIndex = index,
                    sourceIdentity = SessionMethodBinding(state.identity, method.descriptor, method.className, index)) }
        }
    }

    override fun findClasses(typeName: String): List<RuleClass> {
        ensureOpen()
        return state.backends.flatMapIndexed { index, backend ->
            backend.findClasses(typeName).filter { isVisible(it.className, index) }
        }
    }

    private fun issuedIndex(method: RuleMethod, requireVisible: Boolean = true): Int {
        ensureOpen()
        val index = method.sourceDexIndex
        val binding = requireNotNull(method.sourceIdentity as? SessionMethodBinding) { "Method was not issued by an effective session" }
        require(binding.session === state.identity && binding.descriptor == method.descriptor &&
            binding.className == method.className && binding.dexIndex == index) { "Method does not match its session binding" }
        require(index in state.backends.indices) { "Invalid method DEX index" }
        val definingLoader = snapshot.dexes[index].location.loaderId
        require(definingLoader in snapshot.metadata.resolutionOrder(namespaceId)) { "Method source is outside this namespace" }
        require(!requireVisible || isVisible(method.className, index)) { "Method is not from the effective class definition" }
        return index
    }

    override fun readInstructions(method: RuleMethod): List<RuleInstruction> =
        state.backends[issuedIndex(method)].readInstructions(method.descriptor)

    override fun snapshotContext(): RuleSessionSnapshotContext {
        ensureOpen()
        return RuleSessionSnapshotContext(state.snapshotSha256, namespaceId)
    }

    override fun sourceOf(method: RuleMethod): TargetCodeSource {
        val location = snapshot.dexes[issuedIndex(method, requireVisible = false)].location
        val loader = snapshot.metadata.loaders.single { it.id == location.loaderId }
        return TargetCodeSource(location.loaderId, location.dexIndex, loader.dexSha256[location.dexIndex])
    }

    override fun inContextOf(method: RuleMethod): RuleSession = inNamespace(sourceOf(method).loaderId)

    override fun inNamespace(loaderId: String): RuleSession {
        ensureOpen()
        require(loaderId in snapshot.metadata.resolutionOrder(namespaceId)) { "Requested loader is outside this namespace" }
        return EffectiveSession(state, loaderId, ownsState = false)
    }

    override fun close() {
        if (closed) return
        closed = true
        if (ownsState) state.close()
    }
}

private data class SessionMethodBinding(val session: Any, val descriptor: String, val className: String, val dexIndex: Int)
