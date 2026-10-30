package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.code.*
import io.github.nku100.impad.compat.rules.*
import io.github.nku100.impad.rules.SemanticAppCatalogs
import java.io.File
import kotlin.test.*
import kotlinx.serialization.json.*

class CachedSemanticPlanTest {
    private val catalog = SemanticAppCatalogs.forApp("qq")
    private val hash = "a".repeat(64)
    private val containerHash = "b".repeat(64)
    private val loader = object : ClassLoader(null) {}
    private fun observed(content: String = containerHash) = ObservedCodeInputs(
        StartupLoaderLayout("root", listOf(io.github.nku100.impad.rules.CodeLoaderInput("root", null, CodeDelegation.PARENT_FIRST, listOf(File("/ignored.apk")))), mapOf("root" to loader)),
        mapOf("root" to listOf(ObservedCodeContainer(File("/ignored.apk"), content, CodeContainerKind.ZIP))),
    ) { true }
    private fun snapshot() = CodeSnapshot("root", listOf(CodeLoader("root", null, CodeDelegation.PARENT_FIRST, listOf(hash))))
    private fun plan() = catalog.strategies.single().let { strategy ->
        CompletePlan(snapshot().contentSha256(), catalog.contractId, catalog.contractRevision, strategy.id, strategy.revision,
            strategy.roles.map { PlanHook(it.role, "Lsample/Getter;->device()Z", MethodShape(emptyList(), "boolean", true), TargetCodeSource("root", 0, hash), it.phase, it.configuration) })
    }
    private fun cached() = CachedSemanticPlan.create(observed(), snapshot(), listOf(CodeArchiveManifest("root", 0, CodeContainerKind.ZIP, containerHash, listOf(hash))), plan(), catalog)

    @Test
    fun roundTripReconstructsExactSnapshotAndCompletePlanWithoutQueryingCode() {
        val original = cached()
        val restored = assertNotNull(CachedSemanticPlan.decode(original.encode(), observed(), catalog))
        assertEquals(snapshot().contentSha256(), restored.snapshot.contentSha256())
        assertEquals(plan(), restored.plan)
        assertNull(CachedSemanticPlan.decode(original.encode(), observed("c".repeat(64)), catalog))
        assertNull(CachedSemanticPlan.decode(original.encode(), observed(), SemanticAppCatalogs.forApp("wecom")))
    }

    @Test
    fun rejectsMissingReorderedOrContradictoryManifestEvenWithValidJson() {
        val valid = Json.parseToJsonElement(cached().encode()).jsonObject
        val archive = valid.getValue("archives").jsonArray.single().jsonObject
        val corruptions = listOf(
            valid + ("archives" to JsonArray(emptyList())),
            valid + ("archives" to buildJsonArray { add(JsonObject(archive + ("containerIndex" to JsonPrimitive(1)))) }),
            valid + ("archives" to buildJsonArray { add(JsonObject(archive + ("dexSha256" to buildJsonArray { add("f".repeat(64)) }))) }),
            valid + ("schemaVersion" to JsonPrimitive(99)),
            valid + ("inputFingerprint" to JsonPrimitive("f".repeat(64))),
        )
        for (document in corruptions) assertNull(CachedSemanticPlan.decode(JsonObject(document).toString(), observed(), catalog))
        assertNull(CachedSemanticPlan.decode("{", observed(), catalog))
    }

    @Test
    fun freshCaptureCannotBeWrittenUnderAnObservationOfDifferentBytes() {
        assertFailsWith<IllegalArgumentException> {
            CachedSemanticPlan.create(observed(), snapshot(), listOf(CodeArchiveManifest("root", 0, CodeContainerKind.ZIP, "c".repeat(64), listOf(hash))), plan(), catalog)
        }
        val wrongSource = plan().let { plan -> CompletePlan(plan.snapshotSha256, plan.contractId, plan.contractRevision, plan.strategyId, plan.strategyRevision,
            plan.hooks.map { PlanHook(it.role, it.descriptor, it.shape, TargetCodeSource("root", 1, hash), it.phase, it.configuration) }) }
        assertFailsWith<IllegalArgumentException> {
            CachedSemanticPlan.create(observed(), snapshot(), listOf(CodeArchiveManifest("root", 0, CodeContainerKind.ZIP, containerHash, listOf(hash))), wrongSource, catalog)
        }
    }
}
