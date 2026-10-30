package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.compat.rules.HookPhase
import io.github.nku100.impad.compat.rules.RecognitionResult
import io.github.nku100.impad.compat.rules.RecognizedPlan
import io.github.nku100.impad.rules.AnalysisIncompleteException
import io.github.nku100.impad.rules.HookRoleContract
import io.github.nku100.impad.rules.MethodQuery
import io.github.nku100.impad.rules.RecognitionContext
import io.github.nku100.impad.rules.RuleInstructionReference
import io.github.nku100.impad.rules.RuleMethod
import io.github.nku100.impad.rules.RuleSession
import io.github.nku100.impad.rules.RuleStrategyContract
import io.github.nku100.impad.rules.SemanticRuleCatalog
import io.github.nku100.impad.rules.SemanticRuleQuery

object WeChatSemanticRules : SemanticRuleQuery {
    override val id = "wechat_tablet_login_structures"

    val catalog: SemanticRuleCatalog by lazy {
        SemanticRuleCatalog("wechat", "wechat_tablet_login", 1, setOf("tablet", "login"), listOf(this), listOf(
            RuleStrategyContract(STRATEGY, 1, listOf(
                HookRoleContract("tablet", HookPhase.AFTER, mapOf("policy" to "outside_chat_true")),
                HookRoleContract("login", HookPhase.AFTER, mapOf("policy" to "show_hidden_login_view")),
            )),
        ))
    }

    override fun recognize(context: RecognitionContext): RecognitionResult {
        val plans = mutableListOf<RecognizedPlan>()
        val emit: (WeChatLoginPath) -> Unit = { path ->
            plans += context.plan(STRATEGY, mapOf("tablet" to path.wrapper, "login" to path.login), listOf(
                "${path.variant}: ${path.decision.descriptor} -> ${path.wrapper.descriptor} -> ${path.login.descriptor}",
            ))
        }
        try {
            context.snapshot.metadata.resolutionOrder().forEach { namespace ->
                context.session.inNamespace(namespace).use { session ->
                    WeChatTabletLoginRule.forEachCompletePath(session, emit)
                    WeChatTabletLoginRule002.forEachCompletePath(session, emit)
                }
            }
        } catch (incomplete: AnalysisIncompleteException) {
            return RecognitionResult.Incomplete(incomplete.message?.takeIf(String::isNotBlank) ?: "WeChat relationship analysis incomplete", plans)
        }
        return if (plans.isEmpty()) RecognitionResult.NoMatch(listOf("No complete WeChat decision-wrapper-login relationship"))
            else RecognitionResult.Plans(plans)
    }

    private const val STRATEGY = "wechat_tablet_login_callbacks"
}

internal data class WeChatLoginPath(val decision: RuleMethod, val wrapper: RuleMethod, val login: RuleMethod, val variant: String)

internal fun RuleMethod.invocationReference() = RuleInstructionReference(
    className, descriptor.substringAfter("->").substringBefore('('), parameterTypes, returnType,
)

internal fun RuleSession.resolvesInvocation(caller: RuleMethod, target: RuleMethod): Boolean {
    val reference = target.invocationReference()
    if (caller.invokes.none { it.className == reference.owner && it.name == reference.name &&
        it.parameterTypes == reference.parameterTypes && it.returnType == reference.returnType }) return false
    val expected = sourceOf(target)
    return inContextOf(caller).use { callerView ->
        val actual = callerView.findMethods(MethodQuery(declaredClass = target.className, parameterTypes = target.parameterTypes,
            returnType = target.returnType, staticOnly = target.isStatic)).singleOrNull {
            it.descriptor == target.descriptor && it.isStatic == target.isStatic
        }
        actual != null && callerView.sourceOf(actual) == expected
    }
}
