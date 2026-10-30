package io.github.nku100.impad.compat.rules

import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.security.MessageDigest
import java.util.Collections
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.Transient

@Serializable
data class TargetCodeSource(val loaderId: String, val dexIndex: Int, val dexSha256: String) {
    init {
        require(loaderId.isNotBlank() && dexIndex >= 0) { "Invalid target code location" }
        require(dexSha256.matches(PLAN_SHA256)) { "Invalid target DEX hash" }
    }
}

@Serializable
enum class HookPhase { BEFORE, AFTER, AROUND }

@Serializable
class PlanHook private constructor(
    val role: String,
    val descriptor: String,
    @SerialName("shape") private val shapeValue: MethodShape,
    val source: TargetCodeSource,
    val phase: HookPhase,
    @SerialName("configuration") private val configurationValues: Map<String, String>,
    @Transient private val constructorMarker: Boolean = true,
) {
    @Transient val shape: MethodShape = shapeValue.copy(parameters = immutablePlanList(shapeValue.parameters))
    @Transient val configuration: Map<String, String> = Collections.unmodifiableMap(configurationValues.toMap())

    // Copy both constructor and deserialized collections before exposing an execution identity.
    constructor(role: String, descriptor: String, shape: MethodShape, source: TargetCodeSource, phase: HookPhase,
        configuration: Map<String, String> = emptyMap()) : this(role, descriptor,
        shape.copy(parameters = immutablePlanList(shape.parameters)), source, phase, configuration.toMap(), true)

    init {
        require(role.isNotBlank() && descriptor.isNotBlank()) { "A hook needs a role and method descriptor" }
        require(shape.returnType.isNotBlank() && shape.parameters.none(String::isBlank)) { "Invalid method shape" }
        require(configuration.keys.none(String::isBlank)) { "Hook configuration keys must be nonblank" }
    }
}

@Serializable
class CompletePlan private constructor(
    val snapshotSha256: String,
    val contractId: String,
    val contractRevision: Int,
    val strategyId: String,
    val strategyRevision: Int,
    @SerialName("hooks") private val hookValues: List<PlanHook>,
    val schemaVersion: Int,
) {
    @Transient val hooks: List<PlanHook> = immutablePlanList(hookValues)

    constructor(snapshotSha256: String, contractId: String, contractRevision: Int, strategyId: String,
        strategyRevision: Int, hooks: List<PlanHook>) : this(snapshotSha256, contractId, contractRevision,
        strategyId, strategyRevision, hooks.toList(), 1)

    init {
        require(schemaVersion == 1 && snapshotSha256.matches(PLAN_SHA256)) { "Unsupported or invalid complete plan" }
        require(contractId.isNotBlank() && contractRevision > 0 && strategyId.isNotBlank() && strategyRevision > 0) {
            "A complete plan needs versioned behavior and strategy contracts"
        }
        require(hooks.isNotEmpty() && hooks.map { it.role }.distinct().size == hooks.size) { "Hook roles must be nonempty and unique" }
    }

    @Transient private val executionBytes = encodeExecution()
    @Transient private val identity = MessageDigest.getInstance("SHA-256").digest(executionBytes).joinToString("") { "%02x".format(it) }

    fun executionSha256(): String = identity
    override fun equals(other: Any?): Boolean = other is CompletePlan && executionBytes.contentEquals(other.executionBytes)
    override fun hashCode(): Int = executionBytes.contentHashCode()

    private fun encodeExecution(): ByteArray {
        val bytes = ByteArrayOutputStream()
        DataOutputStream(bytes).use { out ->
            out.writeInt(schemaVersion)
            out.planString(snapshotSha256.lowercase())
            out.planString(contractId); out.writeInt(contractRevision)
            out.planString(strategyId); out.writeInt(strategyRevision)
            out.writeInt(hooks.size)
            hooks.forEach { hook ->
                out.planString(hook.role); out.planString(hook.descriptor)
                out.writeInt(hook.shape.parameters.size)
                hook.shape.parameters.forEach(out::planString)
                out.planString(hook.shape.returnType); out.writeBoolean(hook.shape.isStatic)
                out.planString(hook.source.loaderId); out.writeInt(hook.source.dexIndex)
                out.planString(hook.source.dexSha256.lowercase()); out.planString(hook.phase.name)
                out.writeInt(hook.configuration.size)
                hook.configuration.toSortedMap().forEach { (key, value) -> out.planString(key); out.planString(value) }
            }
        }
        return bytes.toByteArray()
    }
}

private val PLAN_SHA256 = Regex("[0-9a-fA-F]{64}")
private fun <T> immutablePlanList(values: List<T>): List<T> = Collections.unmodifiableList(values.toList())
private fun DataOutputStream.planString(value: String) {
    val bytes = value.toByteArray(Charsets.UTF_8)
    writeInt(bytes.size); write(bytes)
}
