package io.github.nku100.impad.runtime

import io.github.nku100.impad.compat.rules.CompletePlan
import java.util.Base64
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

object SemanticRuntimeLog {
    private val json = Json { encodeDefaults = true }

    fun requireLoggable(plan: CompletePlan) {
        requireToken(plan.contractId)
        requireToken(plan.strategyId)
        encodedPlan(plan)
    }

    fun planEvent(plan: CompletePlan, cacheHit: Boolean, attempts: Int, elapsedRealtimeNs: Long): String {
        require(attempts in 1..2 && elapsedRealtimeNs > 0)
        return "rule-runtime event=semantic_plan protocolVersion=2 elapsedRealtimeNs=$elapsedRealtimeNs " +
            "executionSha256=${plan.executionSha256()} plan=${encodedPlan(plan)} cacheHit=$cacheHit attempts=$attempts"
    }

    fun installedEvent(plan: CompletePlan, processName: String, elapsedRealtimeNs: Long): String {
        require(elapsedRealtimeNs > 0)
        requireToken(plan.contractId)
        requireToken(plan.strategyId)
        requireToken(processName)
        return "rule-runtime event=hook_installed protocolVersion=2 elapsedRealtimeNs=$elapsedRealtimeNs " +
            "contractId=${plan.contractId} contractRevision=${plan.contractRevision} " +
            "strategyId=${plan.strategyId} strategyRevision=${plan.strategyRevision} " +
            "executionSha256=${plan.executionSha256()} snapshotSha256=${plan.snapshotSha256} " +
            "hookCount=${plan.hooks.size} processName=$processName"
    }

    private fun encodedPlan(plan: CompletePlan): String {
        val encoded = Base64.getUrlEncoder().withoutPadding().encodeToString(json.encodeToString(plan).toByteArray(Charsets.UTF_8))
        // Leave room for the event envelope within Android's per-record log limit.
        require(encoded.length <= 2800) { "Complete plan exceeds the runtime evidence record limit" }
        return encoded
    }

    private fun requireToken(value: String) = require(value.isNotBlank() && value.none(Char::isWhitespace)) { "Runtime evidence token contains whitespace" }
}
