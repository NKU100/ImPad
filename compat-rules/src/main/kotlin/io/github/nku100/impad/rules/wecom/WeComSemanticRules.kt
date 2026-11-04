package io.github.nku100.impad.rules.wecom

import io.github.nku100.impad.compat.rules.HookPhase
import io.github.nku100.impad.compat.rules.RecognitionResult
import io.github.nku100.impad.rules.HookRoleContract
import io.github.nku100.impad.rules.RecognitionContext
import io.github.nku100.impad.rules.RuleStrategyContract
import io.github.nku100.impad.rules.SemanticRuleCatalog
import io.github.nku100.impad.rules.SemanticRuleQuery

object WeComSemanticRules {
    val catalog = SemanticRuleCatalog(
        appId = "wecom",
        contractId = "wecom_local_tablet",
        contractRevision = 1,
        requiredRoles = setOf("device_type", "app_pad", "service_pad"),
        queries = listOf(LocalFeaturesRecognizer),
        strategies = listOf(
            RuleStrategyContract(
                id = "wecom_local_tablet_callbacks",
                revision = 1,
                roles = listOf(
                    HookRoleContract("device_type", HookPhase.AROUND, mapOf("result" to "131076")),
                    HookRoleContract("app_pad", HookPhase.AROUND, mapOf("result" to "true")),
                    HookRoleContract("service_pad", HookPhase.AROUND, mapOf("result" to "true")),
                ),
            ),
        ),
    )

    private object LocalFeaturesRecognizer : SemanticRuleQuery {
        override val id = "wecom_local_features"

        override fun recognize(context: RecognitionContext): RecognitionResult {
            val matches = WeComLocalRule.matchingMethods(context.session)
            val deviceTypes = matches.getValue("device_type").filter(context::isVisibleFromReady)
            val applicationPads = matches.getValue("app_pad").filter(context::isVisibleFromReady)
            val servicePads = matches.getValue("service_pad").filter(context::isVisibleFromReady)
            val plans = buildList {
                deviceTypes.forEach { deviceType ->
                    applicationPads.forEach { appPad ->
                        servicePads.forEach { servicePad ->
                            add(context.plan(
                                "wecom_local_tablet_callbacks",
                                mapOf(
                                    "device_type" to deviceType,
                                    "app_pad" to appPad,
                                    "service_pad" to servicePad,
                                ),
                                listOf(
                                    "namespace=${context.snapshot.metadata.readyLoaderId}",
                                    "device_type=${deviceType.descriptor}",
                                    "app_pad=${appPad.descriptor}",
                                    "service_pad=${servicePad.descriptor}",
                                ),
                            ))
                        }
                    }
                }
            }
            return if (plans.isEmpty()) RecognitionResult.NoMatch(listOf("No complete WeCom local tablet plan matched"))
            else RecognitionResult.Plans(plans)
        }
    }
}
