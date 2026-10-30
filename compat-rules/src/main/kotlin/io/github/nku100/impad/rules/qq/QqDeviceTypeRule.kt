package io.github.nku100.impad.rules.qq

import io.github.nku100.impad.compat.rules.MethodShape
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.ResolvedTarget
import io.github.nku100.impad.compat.rules.RuleDefinition
import io.github.nku100.impad.compat.rules.RuleRef
import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.compat.rules.VersionBinding
import io.github.nku100.impad.rules.MethodQuery
import io.github.nku100.impad.rules.RuleClass
import io.github.nku100.impad.rules.RuleMethod
import io.github.nku100.impad.rules.RuleCatalog
import io.github.nku100.impad.rules.RuleQuery
import io.github.nku100.impad.rules.RuleSession

object QqDeviceTypeRule : RuleQuery {
    override val definition = RuleDefinition(
        ruleId = "qq_device_type_001",
        strategyId = "qq_tablet_001",
        requiredTargetIds = setOf("device_type"),
    )

    override fun resolve(session: RuleSession): RuleResolution {
        val matches = matchingMethods(session)
        val counts = mapOf("device_type" to matches.size)
        if (matches.isEmpty()) {
            return RuleResolution.Missing(counts, listOf("No definition matched the QQ device-type feature"))
        }
        if (matches.size > 1) {
            return RuleResolution.Ambiguous(counts, listOf("Multiple DEX method definitions matched the QQ device-type feature"))
        }
        val method = matches.single()
        return RuleResolution.Unique(
            plan = ResolvedPlan(
                rule = RuleRef(definition.ruleId, definition.strategyId),
                targets = mapOf(
                    "device_type" to ResolvedTarget(
                        descriptor = method.descriptor,
                        shape = MethodShape(method.parameterTypes, method.returnType, method.isStatic),
                    ),
                ),
            ),
            candidateCounts = counts,
        )
    }

    internal fun matchingMethods(
        session: RuleSession,
        classLookup: (RuleMethod) -> List<RuleClass> = { method -> session.findClasses(method.returnType) },
    ): List<RuleMethod> {
        val anchorCandidates = session.findMethods(
            MethodQuery(
                requiredStrings = setOf("PadUtil", "context is null"),
                parameterTypes = listOf("android.content.Context"),
                staticOnly = true,
            ),
        )
        val matches = anchorCandidates.filter { method ->
            if (method.codeUnitCount == 0) return@filter false
            val enum = classLookup(method)
                .singleOrNull { it.className == method.returnType }
                ?: return@filter false
            if (enum.superClassName != "java.lang.Enum" || enum.fields.none { field ->
                    field.name == "TABLET" && field.accessFlags and (ACC_STATIC or ACC_FINAL or ACC_ENUM) ==
                    (ACC_STATIC or ACC_FINAL or ACC_ENUM)
                }) return@filter false
            val ownReads = method.fields.filter { it.isRead && it.className == method.className }
            ownReads.any { it.typeName == method.returnType } &&
                ownReads.any { it.typeName == "java.util.concurrent.atomic.AtomicBoolean" } &&
                method.invokes.any {
                    it.className == method.className &&
                        it.parameterTypes == listOf("android.content.Context") &&
                        it.returnType == "void"
                }
        }
        return matches
    }

    val catalog: RuleCatalog by lazy {
        RuleCatalog(
            appId = "qq",
            queries = listOf(this),
            bindings = listOf(VersionBinding(ruleId = definition.ruleId, min = 14378, max = null)),
            strategyIds = setOf("qq_tablet_001"),
        )
    }

    private const val ACC_STATIC = 0x0008
    private const val ACC_FINAL = 0x0010
    private const val ACC_ENUM = 0x4000
}
