package io.github.nku100.impad.rules

import io.github.nku100.impad.compat.AppCompatibilityPolicies
import io.github.nku100.impad.compat.AppCompatibilityPolicy
import io.github.nku100.impad.compat.BuildIdentity
import io.github.nku100.impad.compat.rules.CompletePlan
import io.github.nku100.impad.compat.rules.CompletePlanSelection
import io.github.nku100.impad.compat.rules.CompletePlanSelector
import io.github.nku100.impad.compat.rules.HookPhase
import io.github.nku100.impad.compat.rules.MethodShape
import io.github.nku100.impad.compat.rules.PlanHook
import io.github.nku100.impad.compat.rules.RecognitionResult
import io.github.nku100.impad.compat.rules.RecognizedPlan
import io.github.nku100.impad.compat.rules.VersionBinding
import java.security.MessageDigest
import java.util.Collections
import java.util.concurrent.CancellationException
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

interface SemanticRuleQuery {
    val id: String
    fun recognize(context: RecognitionContext): RecognitionResult
}

data class HookRoleContract(val role: String, val phase: HookPhase, val configuration: Map<String, String> = emptyMap())

class RuleStrategyContract(val id: String, val revision: Int, roles: List<HookRoleContract>) {
    val roles: List<HookRoleContract> = Collections.unmodifiableList(roles.map {
        it.copy(configuration = Collections.unmodifiableMap(it.configuration.toMap()))
    })
    init {
        require(id.isNotBlank() && revision > 0) { "Invalid strategy contract" }
        require(this.roles.isNotEmpty() && this.roles.all { it.role.isNotBlank() } &&
            this.roles.map { it.role }.distinct().size == this.roles.size) { "Strategy roles must be nonempty and unique" }
        require(this.roles.all { it.configuration.keys.none(String::isBlank) }) { "Invalid strategy configuration key" }
    }
}

class SemanticRuleCatalog(
    val appId: String,
    val contractId: String,
    val contractRevision: Int,
    requiredRoles: Set<String>,
    queries: List<SemanticRuleQuery>,
    strategies: List<RuleStrategyContract>,
    versionHints: List<VersionBinding> = emptyList(),
    val policy: AppCompatibilityPolicy = AppCompatibilityPolicies.require(appId),
) {
    val requiredRoles: Set<String> = Collections.unmodifiableSet(requiredRoles.toSet())
    val queries: List<SemanticRuleQuery> = Collections.unmodifiableList(queries.map { RegisteredSemanticQuery(it.id, it) })
    val strategies: List<RuleStrategyContract> = Collections.unmodifiableList(strategies.toList())
    val versionHints: List<VersionBinding> = Collections.unmodifiableList(versionHints.toList())
    val sha256: String

    init {
        require(appId == policy.appId && contractId.isNotBlank() && contractRevision > 0) { "Invalid semantic behavior contract" }
        require(this.requiredRoles.isNotEmpty() && this.requiredRoles.none(String::isBlank)) { "Required roles cannot be empty" }
        val queryIds = this.queries.map { it.id }
        require(queryIds.isNotEmpty() && queryIds.none(String::isBlank) && queryIds.distinct().size == queryIds.size) { "Invalid recognizer IDs" }
        require(this.strategies.isNotEmpty() && this.strategies.map { it.id }.distinct().size == this.strategies.size) { "Invalid strategy IDs" }
        require(this.strategies.all { it.roles.map { role -> role.role }.toSet() == this.requiredRoles }) { "Strategy is missing required roles" }
        require(this.versionHints.all { it.ruleId in queryIds && it.min >= 0 && (it.max?.let { max -> max >= it.min } ?: true) }) { "Invalid recognizer version hint" }
        val document = buildJsonObject {
            put("schemaVersion", 1); put("appId", appId); put("contractId", contractId); put("contractRevision", contractRevision)
            put("roles", JsonArray(this@SemanticRuleCatalog.requiredRoles.sorted().map(::JsonPrimitive)))
            put("recognizers", JsonArray(queryIds.map(::JsonPrimitive)))
            put("strategies", buildJsonArray {
                this@SemanticRuleCatalog.strategies.sortedBy { it.id }.forEach { strategy -> add(buildJsonObject {
                    put("id", strategy.id); put("revision", strategy.revision)
                    put("roles", buildJsonArray { strategy.roles.forEach { role -> add(buildJsonObject {
                        put("role", role.role); put("phase", role.phase.name)
                        put("configuration", buildJsonObject { role.configuration.toSortedMap().forEach { (key, value) -> put(key, value) } })
                    }) } })
                }) }
            })
            put("versionHints", buildJsonArray { this@SemanticRuleCatalog.versionHints.forEach { hint -> add(buildJsonObject {
                put("recognizer", hint.ruleId); put("min", hint.min); hint.max?.let { put("max", it) }
            }) } })
        }
        sha256 = MessageDigest.getInstance("SHA-256").digest(document.toString().toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
    }

    fun orderedQueries(versionCode: Long): List<SemanticRuleQuery> {
        val preferred = versionHints.filter { versionCode >= it.min && (it.max?.let { max -> versionCode <= max } ?: true) }.map { it.ruleId }.toSet()
        return queries.filter { it.id in preferred } + queries.filterNot { it.id in preferred }
    }

    fun strategy(id: String): RuleStrategyContract = requireNotNull(strategies.singleOrNull { it.id == id }) { "Unsupported strategy: $id" }
}

class RecognitionContext(val session: RuleSession, val snapshot: CapturedCodeSnapshot, val catalog: SemanticRuleCatalog) {
    private val snapshotSha256 = snapshot.metadata.contentSha256()

    init {
        snapshot.ensureOpen()
        require(session.snapshotContext() == RuleSessionSnapshotContext(snapshotSha256, snapshot.metadata.readyLoaderId)) {
            "Recognition session must belong to the ready snapshot and namespace"
        }
    }

    fun plan(strategyId: String, targets: Map<String, RuleMethod>, evidence: List<String>): RecognizedPlan {
        require(targets.keys == catalog.requiredRoles) { "A complete plan must contain all required roles" }
        val strategy = catalog.strategy(strategyId)
        val hooks = strategy.roles.map { role ->
            val method = targets.getValue(role.role)
            PlanHook(role.role, method.descriptor, MethodShape(method.parameterTypes, method.returnType, method.isStatic),
                session.sourceOf(method), role.phase, role.configuration)
        }
        return RecognizedPlan(CompletePlan(snapshotSha256, catalog.contractId, catalog.contractRevision, strategy.id, strategy.revision, hooks), evidence.toList())
    }

    fun validate(plan: CompletePlan) {
        snapshot.ensureOpen()
        require(plan.snapshotSha256.equals(snapshotSha256, ignoreCase = true) && plan.contractId == catalog.contractId &&
            plan.contractRevision == catalog.contractRevision) { "Plan does not match this snapshot and behavior contract" }
        val strategy = catalog.strategy(plan.strategyId)
        require(plan.strategyRevision == strategy.revision && plan.hooks.map { it.role } == strategy.roles.map { it.role }) { "Plan strategy or ordered roles differ" }
        plan.hooks.zip(strategy.roles).forEach { (hook, expected) ->
            require(hook.phase == expected.phase && hook.configuration == expected.configuration) { "Plan changed the callback phase or configuration" }
            val classDescriptor = hook.descriptor.substringBefore("->")
            require(classDescriptor.startsWith('L') && classDescriptor.endsWith(';')) { "Invalid hook declaring class" }
            val className = classDescriptor.removePrefix("L").removeSuffix(";").replace('/', '.')
            session.inNamespace(hook.source.loaderId).use { targetSession ->
                val matches = targetSession.findMethods(MethodQuery(declaredClass = className, parameterTypes = hook.shape.parameters,
                    returnType = hook.shape.returnType, staticOnly = hook.shape.isStatic)).filter {
                    it.descriptor == hook.descriptor && it.isStatic == hook.shape.isStatic &&
                        it.parameterTypes == hook.shape.parameters && it.returnType == hook.shape.returnType
                }
                val method = requireNotNull(matches.singleOrNull()) { "Hook method is missing or ambiguous in the effective view" }
                val source = targetSession.sourceOf(method)
                require(source.loaderId == hook.source.loaderId && source.dexIndex == hook.source.dexIndex &&
                    source.dexSha256.equals(hook.source.dexSha256, ignoreCase = true)) { "Hook source differs from the effective snapshot" }
            }
        }
    }
}

class AnalysisIncompleteException(message: String) : RuntimeException(message)

data class SemanticRuleRun(val evaluatedRecognizerIds: List<String>, val results: Map<String, RecognitionResult>, val outcome: CompletePlanSelection)

object SemanticRuleResolver {
    fun resolveDetailed(identity: BuildIdentity, catalog: SemanticRuleCatalog, context: RecognitionContext): SemanticRuleRun {
        catalog.policy.validateIdentity(identity)
        require(context.catalog === catalog) { "Recognition context belongs to another catalog" }
        context.snapshot.ensureOpen()
        val ordered = catalog.orderedQueries(identity.versionCode)
        val results = linkedMapOf<String, RecognitionResult>()
        val evaluated = mutableListOf<String>()
        fun finish() = SemanticRuleRun(evaluated.toList(), Collections.unmodifiableMap(results.toMap()), CompletePlanSelector.select(results))
        fun cancelFrom(index: Int, reason: String): SemanticRuleRun {
            ordered.drop(index).forEach { results[it.id] = RecognitionResult.Incomplete(reason) }
            return finish()
        }
        if (Thread.currentThread().isInterrupted) return cancelFrom(0, "Thread interrupted before recognition")
        ordered.forEachIndexed { index, query ->
            if (Thread.currentThread().isInterrupted) return cancelFrom(index, "Thread interrupted before recognition")
            evaluated += query.id
            val result = try {
                query.recognize(context).also { recognized ->
                    when (recognized) {
                        is RecognitionResult.Plans -> recognized.plans.forEach { context.validate(it.plan) }
                        is RecognitionResult.Incomplete -> recognized.discoveredPlans.forEach { context.validate(it.plan) }
                        else -> Unit
                    }
                }
            } catch (error: InterruptedException) {
                Thread.currentThread().interrupt()
                return cancelFrom(index, "Interrupted recognition: ${error.message.orEmpty()}")
            } catch (error: CancellationException) {
                return cancelFrom(index, "Cancelled recognition: ${error.message.orEmpty()}")
            } catch (error: AnalysisIncompleteException) {
                RecognitionResult.Incomplete(error.message?.takeIf(String::isNotBlank) ?: "Analysis did not complete")
            } catch (error: UnsupportedOperationException) {
                RecognitionResult.UnsupportedInput(listOf(error.message.orEmpty()))
            } catch (error: Exception) {
                RecognitionResult.EvaluationError(listOf("Recognizer ${query.id} failed: ${error.javaClass.simpleName}: ${error.message.orEmpty()}"))
            }
            if (Thread.currentThread().isInterrupted) {
                val discovered = when (result) {
                    is RecognitionResult.Plans -> result.plans
                    is RecognitionResult.Incomplete -> result.discoveredPlans
                    else -> emptyList()
                }
                results[query.id] = when (result) {
                    is RecognitionResult.EvaluationError, is RecognitionResult.UnsupportedInput -> result
                    else -> RecognitionResult.Incomplete("Thread interrupted during recognition", discovered)
                }
                return cancelFrom(index + 1, "Thread interrupted before recognition")
            }
            results[query.id] = result
        }
        return finish()
    }
}

private class RegisteredSemanticQuery(override val id: String, private val query: SemanticRuleQuery) : SemanticRuleQuery {
    override fun recognize(context: RecognitionContext): RecognitionResult = query.recognize(context)
}
