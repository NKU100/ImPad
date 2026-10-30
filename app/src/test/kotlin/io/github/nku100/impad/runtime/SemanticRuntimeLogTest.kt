package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.rules.*
import java.util.Base64
import kotlinx.serialization.json.Json
import org.junit.jupiter.api.Test
import kotlin.test.*

class SemanticRuntimeLogTest {
    private fun plan(roles: List<String> = listOf("tablet", "login"), configuration: Map<String, String> = emptyMap()): CompletePlan = CompletePlan(
        "a".repeat(64), "contract", 1, "strategy", 1, roles.map { role ->
            PlanHook(role, "Lfixture/Target;->${role}()Z", MethodShape(emptyList(), "boolean", true),
                TargetCodeSource("loader-0", 0, "b".repeat(64)), HookPhase.AFTER, configuration)
        })
    private fun fields(event: String) = event.removePrefix("rule-runtime ").split(' ').associate {
        val (key, value) = it.split('=', limit = 2); key to value
    }

    @Test
    fun planEventCarriesRoundTrippableCompleteExecutionWithoutWhitespace() {
        val original = plan(configuration = mapOf("policy" to "原方法完成后返回 true"))
        val event = fields(SemanticRuntimeLog.planEvent(original, true, 2, 123))
        assertEquals("semantic_plan", event["event"])
        assertEquals("2", event["protocolVersion"])
        assertEquals("true", event["cacheHit"])
        assertEquals("2", event["attempts"])
        assertEquals(original.executionSha256(), event["executionSha256"])
        val decoded = Json.decodeFromString<CompletePlan>(Base64.getUrlDecoder().decode(event.getValue("plan")).decodeToString())
        assertEquals(original, decoded)
    }

    @Test
    fun installedEventBindsEveryBehaviorAndSourceIdentityField() {
        val original = plan(listOf("device_type", "app_pad", "service_pad"))
        val event = fields(SemanticRuntimeLog.installedEvent(original, "com.tencent.mobileqq:MSF", 456))
        assertEquals("hook_installed", event["event"])
        assertEquals("2", event["protocolVersion"])
        assertEquals(original.contractId, event["contractId"])
        assertEquals(original.contractRevision.toString(), event["contractRevision"])
        assertEquals(original.strategyId, event["strategyId"])
        assertEquals(original.strategyRevision.toString(), event["strategyRevision"])
        assertEquals(original.executionSha256(), event["executionSha256"])
        assertEquals(original.snapshotSha256, event["snapshotSha256"])
        assertEquals("3", event["hookCount"])
        assertEquals("com.tencent.mobileqq:MSF", event["processName"])
        assertTrue(SemanticRuntimeLog.planEvent(original, false, 1, 123).toByteArray().size < 3800)
    }

    @Test
    fun unloggablePlansAndInvalidFieldsAreRejectedBeforeRegistration() {
        assertFailsWith<IllegalArgumentException> { SemanticRuntimeLog.requireLoggable(plan(configuration = mapOf("large" to "x".repeat(4000)))) }
        assertFailsWith<IllegalArgumentException> { SemanticRuntimeLog.planEvent(plan(), false, 3, 123) }
        assertFailsWith<IllegalArgumentException> { SemanticRuntimeLog.installedEvent(plan(), "wrong process", 123) }
    }
}
