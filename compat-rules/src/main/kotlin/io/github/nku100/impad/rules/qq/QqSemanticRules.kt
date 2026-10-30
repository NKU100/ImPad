package io.github.nku100.impad.rules.qq

import io.github.nku100.impad.compat.rules.HookPhase
import io.github.nku100.impad.compat.rules.RecognitionResult
import io.github.nku100.impad.rules.HookRoleContract
import io.github.nku100.impad.rules.RecognitionContext
import io.github.nku100.impad.rules.RuleStrategyContract
import io.github.nku100.impad.rules.SemanticRuleCatalog
import io.github.nku100.impad.rules.SemanticRuleQuery

object QqSemanticRules {
    val catalog = SemanticRuleCatalog(
        appId = "qq",
        contractId = "qq_tablet_device_type",
        contractRevision = 1,
        requiredRoles = setOf("device_type"),
        queries = listOf(CachedDeviceEnumRecognizer),
        strategies = listOf(
            RuleStrategyContract(
                id = "qq_tablet_enum",
                revision = 1,
                roles = listOf(HookRoleContract("device_type", HookPhase.AROUND, mapOf("enum" to "TABLET"))),
            ),
        ),
    )

    private object CachedDeviceEnumRecognizer : SemanticRuleQuery {
        override val id = "qq_cached_device_enum"

        override fun recognize(context: RecognitionContext): RecognitionResult {
            val plans = context.snapshot.metadata.loaders.flatMap { loader ->
                context.session.inNamespace(loader.id).use { namespace ->
                    QqDeviceTypeRule.matchingMethods(namespace) { method ->
                        namespace.inContextOf(method).use { origin -> origin.findClasses(method.returnType) }
                    }.map { method ->
                        context.plan("qq_tablet_enum", mapOf("device_type" to method), listOf(
                            "namespace=${loader.id}",
                            "device_type=${context.session.sourceOf(method).loaderId}:${method.descriptor}",
                        ))
                    }
                }
            }
            return if (plans.isEmpty()) RecognitionResult.NoMatch(listOf("No complete QQ tablet enum plan matched"))
            else RecognitionResult.Plans(plans)
        }
    }
}
