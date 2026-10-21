package io.github.nku100.impad.rules.wecom

import io.github.nku100.impad.compat.rules.MethodShape
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.ResolvedTarget
import io.github.nku100.impad.compat.rules.RuleDefinition
import io.github.nku100.impad.compat.rules.RuleRef
import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.compat.rules.VersionBinding
import io.github.nku100.impad.rules.MethodQuery
import io.github.nku100.impad.rules.RuleCatalog
import io.github.nku100.impad.rules.RuleMethod
import io.github.nku100.impad.rules.RuleQuery
import io.github.nku100.impad.rules.RuleSession

object WeComLocalRule : RuleQuery {
    private val TARGET_IDS = setOf("device_type", "app_pad", "service_pad")

    override val definition = RuleDefinition(
        ruleId = RULE_ID,
        strategyId = STRATEGY_ID,
        requiredTargetIds = TARGET_IDS,
    )

    override fun resolve(session: RuleSession): RuleResolution {
        val deviceType = session.findMethods(
            MethodQuery(
                parameterTypes = emptyList(),
                declaredClass = APPLICATION,
                returnType = "int",
            ),
        ).filter(::isLocalDeviceType)
        val applicationPad = session.findMethods(
            MethodQuery(
                parameterTypes = emptyList(),
                staticOnly = true,
                declaredClass = APPLICATION,
                returnType = "boolean",
            ),
        ).filter(::isApplicationPad)
        val servicePad = session.findMethods(
            MethodQuery(
                parameterTypes = emptyList(),
                declaredClass = SERVICE,
                returnType = "boolean",
            ),
        ).filter(::isServicePad)

        val matches = linkedMapOf(
            "device_type" to deviceType,
            "app_pad" to applicationPad,
            "service_pad" to servicePad,
        )
        val counts = matches.mapValues { it.value.size }
        if (matches.values.any { it.size > 1 }) {
            return RuleResolution.Ambiguous(
                counts,
                listOf("Multiple DEX method definitions matched a WeCom local tablet feature"),
            )
        }
        if (matches.values.any { it.isEmpty() }) {
            return RuleResolution.Missing(
                counts,
                listOf("A required WeCom local tablet feature was missing"),
            )
        }

        return RuleResolution.Unique(
            plan = ResolvedPlan(
                rule = RuleRef(RULE_ID, STRATEGY_ID),
                targets = matches.mapValues { (_, definitions) -> definitions.single().toTarget() },
            ),
            candidateCounts = counts,
        )
    }

    private fun isLocalDeviceType(method: RuleMethod): Boolean {
        if (method.methodName() != "getDeviceTypeFromLocal") return false
        val providerCalls = method.invokes.filter {
            it.className == APPLICATION && it.name == "nativeGetDeviceTypeFromLocal" &&
                it.parameterTypes.isEmpty() && it.returnType == "int"
        }
        return method.className == APPLICATION && method.parameterTypes.isEmpty() &&
            method.returnType == "int" && !method.isStatic && method.codeUnitCount == 3 &&
            providerCalls.size == 1 && method.invokes.size == 1
    }

    private fun isApplicationPad(method: RuleMethod): Boolean {
        val serviceFields = method.fields.filter {
            it.className == APPLICATION && it.name == "mApplicationApi" &&
                it.typeName == SERVICE_API && it.isRead
        }
        val serviceCalls = method.invokes.filter {
            it.className == SERVICE_API && it.name == "isAndroidPad" &&
                it.parameterTypes.isEmpty() && it.returnType == "boolean"
        }
        return method.methodName() == "isAndroidPad" && method.className == APPLICATION &&
            method.parameterTypes.isEmpty() &&
            method.returnType == "boolean" && method.isStatic && serviceFields.size == 1 &&
            serviceCalls.size == 1 && method.invokes.size == 1
    }

    private fun isServicePad(method: RuleMethod): Boolean {
        if (method.methodName() != "isAndroidPad" || method.className != SERVICE ||
            method.parameterTypes.isNotEmpty() || method.returnType != "boolean" ||
            method.isStatic || method.codeUnitCount != 10 ||
            method.invokes.size !in 2..3) return false

        val predicateCalls = method.invokes.filter {
            it.name.isNotBlank() && it.parameterTypes.isEmpty() && it.returnType == "boolean"
        }
        val provider = predicateCalls.singleOrNull()?.className ?: return false
        val providerAccessors = method.invokes.count {
            it.className == provider && it.name.isNotBlank() &&
                it.parameterTypes.isEmpty() && it.returnType == provider
        }
        return providerAccessors in 1..2 && method.invokes.all {
            it.className == provider && it.name.isNotBlank() && it.parameterTypes.isEmpty() &&
                (it.returnType == provider || it.returnType == "boolean")
        }
    }

    private fun RuleMethod.methodName(): String =
        descriptor.substringAfter("->", "").substringBefore('(')

    private fun RuleMethod.toTarget() = ResolvedTarget(
        descriptor = descriptor,
        shape = MethodShape(parameterTypes, returnType, isStatic),
    )

    val catalog: RuleCatalog by lazy {
        RuleCatalog(
            appId = "wecom",
            queries = listOf(this),
            bindings = listOf(VersionBinding(ruleId = RULE_ID, min = 70675, max = null)),
            strategyIds = setOf(STRATEGY_ID),
        )
    }

    private const val RULE_ID = "wecom_local_001"
    private const val STRATEGY_ID = "wecom_local_tablet_001"
    private const val APPLICATION = "com.tencent.wework.foundation.logic.Application"
    private const val SERVICE_API = "com.tencent.wework.foundation.logic.IWeworkServiceApi"
    private const val SERVICE = "com.tencent.wework.foundation.impl.WeworkServiceImpl"
}
