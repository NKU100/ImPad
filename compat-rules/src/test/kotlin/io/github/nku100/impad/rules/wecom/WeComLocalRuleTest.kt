package io.github.nku100.impad.rules.wecom

import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.rules.MethodQuery
import io.github.nku100.impad.rules.RuleCatalog
import io.github.nku100.impad.rules.RuleClass
import io.github.nku100.impad.rules.RuleFieldReference
import io.github.nku100.impad.rules.RuleMethod
import io.github.nku100.impad.rules.RuleMethodReference
import io.github.nku100.impad.rules.RuleSession
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertNull
import kotlin.test.assertTrue

class WeComLocalRuleTest {
    @Test
    fun registersOnlyTheFirstLocalRuleAndKeepsTheExistingQqCatalogIdentity() {
        val catalog = RuleCatalog.forApp("wecom")
        val binding = catalog.bindings.single()

        assertEquals("wecom", catalog.appId)
        assertEquals(setOf("wecom_local_001"), catalog.definitions.map { it.ruleId }.toSet())
        assertEquals("wecom_local_tablet_001", catalog.definitions.single().strategyId)
        assertEquals(setOf("device_type", "app_pad", "service_pad"), catalog.definitions.single().requiredTargetIds)
        assertEquals(70675L, binding.min)
        assertNull(binding.max)
        assertEquals(setOf("wecom_local_tablet_001"), catalog.strategyIds)
        assertEquals(
            "cc3a3cbf6cfdaea6aedf0e27b6154af544a59550d8e86cfa5d663b4ccfaef66a",
            RuleCatalog.forApp("qq").sha256,
        )
    }

    @Test
    fun resolvesTheThreeUniqueTargetsFromTheirCodeStructureWithoutStringAnchors() {
        val session = FixtureSession(listOf(listOf(localDeviceType()), listOf(applicationPad()), listOf(servicePad())))
        val query = requireNotNull(RuleCatalog.forApp("wecom").query("wecom_local_001"))

        val result = assertIs<RuleResolution.Unique>(query.resolve(session))

        assertEquals(mapOf("device_type" to 1, "app_pad" to 1, "service_pad" to 1), result.candidateCounts)
        assertEquals(
            setOf("device_type", "app_pad", "service_pad"),
            result.plan.targets.keys,
        )
        assertEquals(
            "Lcom/tencent/wework/foundation/logic/Application;->getDeviceTypeFromLocal()I",
            result.plan.targets.getValue("device_type").descriptor,
        )
        assertTrue(session.queries.all { it.requiredStrings.isEmpty() })
        assertEquals(
            listOf(
                "com.tencent.wework.foundation.logic.Application",
                "com.tencent.wework.foundation.logic.Application",
                "com.tencent.wework.foundation.impl.WeworkServiceImpl",
            ),
            session.queries.map { queryProperty(it, "declaredClass") },
        )
        assertEquals(listOf("int", "boolean", "boolean"), session.queries.map { queryProperty(it, "returnType") })
    }

    @Test
    fun doesNotProduceAPlanWhenAnyRequiredStructuralFeatureIsMissing() {
        val session = FixtureSession(
            listOf(
                listOf(localDeviceType().copy(invokes = emptyList())),
                listOf(applicationPad()),
                listOf(servicePad()),
            ),
        )
        val query = requireNotNull(RuleCatalog.forApp("wecom").query("wecom_local_001"))

        val result = assertIs<RuleResolution.Missing>(query.resolve(session))

        assertEquals(mapOf("device_type" to 0, "app_pad" to 1, "service_pad" to 1), result.candidateCounts)
    }

    @Test
    fun preservesAmbiguityWhenAFeatureHasTwoDefinitions() {
        val session = FixtureSession(
            listOf(
                listOf(localDeviceType()),
                listOf(applicationPad()),
                listOf(servicePad(), servicePad()),
            ),
        )
        val query = requireNotNull(RuleCatalog.forApp("wecom").query("wecom_local_001"))

        val result = assertIs<RuleResolution.Ambiguous>(query.resolve(session))

        assertEquals(mapOf("device_type" to 1, "app_pad" to 1, "service_pad" to 2), result.candidateCounts)
    }

    @Test
    fun matchesServicePadWhenDexKitReturnsDistinctInvokedMethodReferences() {
        val distinctServiceReferences = servicePad().copy(invokes = servicePad().invokes.distinct())
        val session = FixtureSession(listOf(
            listOf(localDeviceType()),
            listOf(applicationPad()),
            listOf(distinctServiceReferences),
        ))
        val query = requireNotNull(RuleCatalog.forApp("wecom").query("wecom_local_001"))

        val result = assertIs<RuleResolution.Unique>(query.resolve(session))

        assertEquals(mapOf("device_type" to 1, "app_pad" to 1, "service_pad" to 1), result.candidateCounts)
    }

    @Test
    fun matchesServicePadThroughProviderStructureWithoutPinningObfuscatedNames() {
        val provider = "renamed.Provider"
        val service = servicePad().copy(invokes = listOf(
            RuleMethodReference(provider, "instance", emptyList(), provider),
            RuleMethodReference(provider, "instance", emptyList(), provider),
            RuleMethodReference(provider, "isPad", emptyList(), "boolean"),
        ))
        val session = FixtureSession(listOf(listOf(localDeviceType()), listOf(applicationPad()), listOf(service)))
        val query = requireNotNull(RuleCatalog.forApp("wecom").query("wecom_local_001"))

        val result = assertIs<RuleResolution.Unique>(query.resolve(session))

        assertEquals(1, result.candidateCounts.getValue("service_pad"))
    }

    @Test
    fun rejectsServicePadWithDifferentInstructionShapeOrUnrelatedInvoke() {
        val query = requireNotNull(RuleCatalog.forApp("wecom").query("wecom_local_001"))
        val invalidMethods = listOf(
            servicePad().copy(codeUnitCount = 9),
            servicePad().copy(invokes = servicePad().invokes +
                RuleMethodReference("unrelated.Provider", "irrelevant", emptyList(), "int")),
        )

        invalidMethods.forEach { invalidServicePad ->
            val session = FixtureSession(listOf(
                listOf(localDeviceType()),
                listOf(applicationPad()),
                listOf(invalidServicePad),
            ))
            val result = assertIs<RuleResolution.Missing>(query.resolve(session))
            assertEquals(0, result.candidateCounts["service_pad"])
        }
    }

    @Test
    fun sameShapeTargetsWithDifferentSemanticNamesDoNotProduceAPlan() {
        val query = requireNotNull(RuleCatalog.forApp("wecom").query("wecom_local_001"))
        val distractors = listOf(
            listOf(localDeviceType().copy(descriptor = localDeviceType().descriptor.replace(
                "getDeviceTypeFromLocal", "getOtherDeviceType"))),
            listOf(applicationPad().copy(descriptor = applicationPad().descriptor.replace(
                "isAndroidPad", "isOtherFlag"))),
            listOf(servicePad().copy(descriptor = servicePad().descriptor.replace(
                "isAndroidPad", "isOtherPad"))),
        )

        distractors.forEachIndexed { index, replacement ->
            val methods = listOf(listOf(localDeviceType()), listOf(applicationPad()), listOf(servicePad()))
                .toMutableList()
            methods[index] = replacement
            val result = assertIs<RuleResolution.Missing>(query.resolve(FixtureSession(methods)))
            assertEquals(0, result.candidateCounts.values.min())
        }
    }

    @Test
    fun referencesRetainMethodAndFieldNamesNeededForSemanticMatching() {
        assertTrue(RuleMethodReference::class.java.declaredFields.any { it.name == "name" })
        assertTrue(RuleFieldReference::class.java.declaredFields.any { it.name == "name" })
    }

    @Test
    fun sameShapeReferencesWithDifferentDeviceAndApiSemanticsDoNotProduceAPlan() {
        val query = requireNotNull(RuleCatalog.forApp("wecom").query("wecom_local_001"))
        val cases = listOf(
            Triple(0, listOf(localDeviceType().copy(invokes = listOf(
                RuleMethodReference(APPLICATION, "getDeviceTypeFromLocal", emptyList(), "int"),
            ))), "device_type"),
            Triple(1, listOf(applicationPad().copy(fields = applicationPad().fields.map {
                it.copy(name = "mOtherApi")
            })), "app_pad"),
            Triple(1, listOf(applicationPad().copy(invokes = listOf(
                RuleMethodReference(SERVICE_API, "otherBoolean", emptyList(), "boolean"),
            ))), "app_pad"),
        )

        cases.forEach { (index, replacement, targetId) ->
            val methods = listOf(listOf(localDeviceType()), listOf(applicationPad()), listOf(servicePad()))
                .toMutableList()
            methods[index] = replacement
            val result = assertIs<RuleResolution.Missing>(query.resolve(FixtureSession(methods)))
            assertEquals(0, result.candidateCounts[targetId])
        }
    }

    private fun queryProperty(query: MethodQuery, property: String): String? =
        query.javaClass.getMethod("get${property.replaceFirstChar(Char::uppercase)}").invoke(query) as? String

    private fun localDeviceType() = RuleMethod(
        descriptor = "Lcom/tencent/wework/foundation/logic/Application;->getDeviceTypeFromLocal()I",
        className = APPLICATION,
        parameterTypes = emptyList(),
        returnType = "int",
        isStatic = false,
        codeUnitCount = 3,
        strings = emptySet(),
        fields = emptyList(),
        invokes = listOf(RuleMethodReference(APPLICATION, "nativeGetDeviceTypeFromLocal", emptyList(), "int")),
    )

    private fun applicationPad() = RuleMethod(
        descriptor = "Lcom/tencent/wework/foundation/logic/Application;->isAndroidPad()Z",
        className = APPLICATION,
        parameterTypes = emptyList(),
        returnType = "boolean",
        isStatic = true,
        codeUnitCount = 6,
        strings = emptySet(),
        fields = listOf(RuleFieldReference(
            className = APPLICATION,
            name = "mApplicationApi",
            typeName = SERVICE_API,
            descriptor = "L$APPLICATION;->mApplicationApi:L$SERVICE_API;",
            isRead = true,
        )),
        invokes = listOf(RuleMethodReference(SERVICE_API, "isAndroidPad", emptyList(), "boolean")),
    )

    private fun servicePad() = RuleMethod(
        descriptor = "Lcom/tencent/wework/foundation/impl/WeworkServiceImpl;->isAndroidPad()Z",
        className = SERVICE,
        parameterTypes = emptyList(),
        returnType = "boolean",
        isStatic = false,
        codeUnitCount = 10,
        strings = emptySet(),
        fields = emptyList(),
        invokes = listOf(
            RuleMethodReference(PAD_PROVIDER, "f", emptyList(), PAD_PROVIDER),
            RuleMethodReference(PAD_PROVIDER, "f", emptyList(), PAD_PROVIDER),
            RuleMethodReference(PAD_PROVIDER, "l", emptyList(), "boolean"),
        ),
    )

    private class FixtureSession(private val responses: List<List<RuleMethod>>) : RuleSession {
        val queries = mutableListOf<MethodQuery>()
        private var nextResponse = 0

        override fun findMethods(query: MethodQuery): List<RuleMethod> {
            queries += query
            return responses[nextResponse++]
        }

        override fun findClasses(typeName: String): List<RuleClass> = emptyList()

        override fun close() = Unit
    }

    private companion object {
        const val APPLICATION = "com.tencent.wework.foundation.logic.Application"
        const val SERVICE_API = "com.tencent.wework.foundation.logic.IWeworkServiceApi"
        const val SERVICE = "com.tencent.wework.foundation.impl.WeworkServiceImpl"
        const val PAD_PROVIDER = "wdz"
    }
}
