package io.github.nku100.impad.rules

import io.github.nku100.impad.compat.code.CodeDelegation
import io.github.nku100.impad.compat.rules.CompletePlanSelection
import io.github.nku100.impad.compat.rules.CompletePlanSelector
import io.github.nku100.impad.compat.rules.HookPhase
import io.github.nku100.impad.compat.rules.RecognitionResult
import io.github.nku100.impad.rules.qq.QqSemanticRules
import io.github.nku100.impad.rules.wecom.WeComSemanticRules
import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

class SemanticDeviceRulesTest {
    @Test
    fun catalogsExposeTheSpecifiedContractsRolesAndConfiguration() {
        val qq = QqSemanticRules.catalog
        assertEquals("qq", qq.appId)
        assertEquals("qq_tablet_device_type", qq.contractId)
        assertEquals(1, qq.contractRevision)
        assertEquals(listOf("qq_cached_device_enum"), qq.queries.map { it.id })
        val qqStrategy = qq.strategy("qq_tablet_enum")
        assertEquals(1, qqStrategy.revision)
        assertEquals(listOf("device_type"), qqStrategy.roles.map { it.role })
        assertEquals(HookPhase.AROUND, qqStrategy.roles.single().phase)
        assertEquals(mapOf("enum" to "TABLET"), qqStrategy.roles.single().configuration)

        val wecom = WeComSemanticRules.catalog
        assertEquals("wecom", wecom.appId)
        assertEquals("wecom_local_tablet", wecom.contractId)
        assertEquals(1, wecom.contractRevision)
        assertEquals(listOf("wecom_local_features"), wecom.queries.map { it.id })
        val wecomStrategy = wecom.strategy("wecom_local_tablet_callbacks")
        assertEquals(1, wecomStrategy.revision)
        assertEquals(listOf("device_type", "app_pad", "service_pad"), wecomStrategy.roles.map { it.role })
        assertEquals(listOf(HookPhase.AROUND, HookPhase.AROUND, HookPhase.AROUND), wecomStrategy.roles.map { it.phase })
        assertEquals(listOf(mapOf("result" to "131076"), mapOf("result" to "true"), mapOf("result" to "true")),
            wecomStrategy.roles.map { it.configuration })
    }

    @Test
    fun qqRecognizesACompleteTabletEnumPlanAndRejectsMissingTabletEnumConstant() = withContext(
        QqSemanticRules.catalog,
        listOf("root" to listOf("fixture.QqProbe", "fixture.DeviceType")),
        listOf(qqMethod()),
        mapOf("fixture.DeviceType" to listOf(tabletEnum())),
    ) { context ->
        val complete = assertIs<RecognitionResult.Plans>(qqResult(context))
        assertEquals(1, complete.plans.size)
        assertEquals("qq_tablet_device_type", complete.plans.single().plan.contractId)
        assertEquals("qq_tablet_enum", complete.plans.single().plan.strategyId)
        assertEquals("root", complete.plans.single().plan.hooks.single().source.loaderId)

        val missingTablet = withContextResult(
            QqSemanticRules.catalog,
            listOf("root" to listOf("fixture.QqProbe", "fixture.DeviceType")),
            listOf(qqMethod()),
            mapOf("fixture.DeviceType" to listOf(RuleClass("fixture.DeviceType", "java.lang.Enum", emptyList()))),
        ) { qqResult(it) }
        assertIs<RecognitionResult.NoMatch>(missingTablet)
    }

    @Test
    fun qqEnumeratesEveryCompleteCandidateInsteadOfRejectingCandidateCounts() = withContext(
        QqSemanticRules.catalog,
        listOf("root" to listOf("fixture.QqProbe", "fixture.QqProbe2", "fixture.DeviceType")),
        listOf(qqMethod(), qqMethod("fixture.QqProbe2")),
        mapOf("fixture.DeviceType" to listOf(tabletEnum())),
    ) { context ->
        val plans = assertIs<RecognitionResult.Plans>(qqResult(context)).plans
        assertEquals(2, plans.size)
        assertEquals(setOf("Lfixture/QqProbe;->deviceType(Landroid/content/Context;)Lfixture/DeviceType;",
            "Lfixture/QqProbe2;->deviceType(Landroid/content/Context;)Lfixture/DeviceType;"),
            plans.map { it.plan.hooks.single().descriptor }.toSet())
        assertIs<CompletePlanSelection.Conflict>(CompletePlanSelector.select(mapOf("qq_cached_device_enum" to RecognitionResult.Plans(plans))))
    }

    @Test
    fun qqMergesEvidenceWhenOneDefinitionIsVisibleInSeveralNamespaces() = withContext(
        QqSemanticRules.catalog,
        listOf(
            "parent" to listOf("fixture.QqProbe", "fixture.DeviceType"),
            "child" to listOf("fixture.ChildOnly"),
        ),
        listOf(qqMethod()),
        mapOf("fixture.DeviceType" to listOf(tabletEnum())),
    ) { context ->
        val plans = assertIs<RecognitionResult.Plans>(qqResult(context)).plans
        val selected = assertIs<CompletePlanSelection.Selected>(CompletePlanSelector.select(
            mapOf("qq_cached_device_enum" to RecognitionResult.Plans(plans)),
        ))
        assertEquals(setOf("parent", "child"), selected.match.evidenceByRecognizer.getValue("qq_cached_device_enum")
            .filter { it.startsWith("namespace=") }.map { it.substringAfter("namespace=") }.toSet())
        assertEquals("parent", selected.match.plan.hooks.single().source.loaderId)
    }

    @Test
    fun wecomRequiresAllThreeLocalFeaturesAndEnumeratesEveryCompleteCombination() = withContext(
        WeComSemanticRules.catalog,
        listOf("root" to wecomClasses()),
        listOf(localDeviceType(), applicationPad(), servicePad()),
        emptyMap(),
    ) { context ->
        val complete = assertIs<RecognitionResult.Plans>(wecomResult(context))
        assertEquals(1, complete.plans.size)
        assertEquals(listOf("device_type", "app_pad", "service_pad"), complete.plans.single().plan.hooks.map { it.role })

        val missingService = withContextResult(
            WeComSemanticRules.catalog,
            listOf("root" to wecomClasses()),
            listOf(localDeviceType(), applicationPad()),
            emptyMap(),
        ) { wecomResult(it) }
        assertIs<RecognitionResult.NoMatch>(missingService)
    }

    @Test
    fun wecomReturnsAllCompletePlansWhenOneFeatureHasMultipleDefinitions() = withContext(
        WeComSemanticRules.catalog,
        listOf("root" to wecomClasses(), "child" to wecomClasses()),
        listOf(localDeviceType(), localDeviceType("getDeviceTypeFromLocal2"), applicationPad(), servicePad()),
        emptyMap(),
    ) { context ->
        val plans = assertIs<RecognitionResult.Plans>(wecomResult(context)).plans
        assertEquals(2, plans.size)
        assertEquals(setOf("root", "child"), plans.map { it.plan.hooks.first().source.loaderId }.toSet())
        assertIs<CompletePlanSelection.Conflict>(CompletePlanSelector.select(mapOf("wecom_local_features" to RecognitionResult.Plans(plans))))
    }

    @Test
    fun wecomNeverInstallsAPlanWithOnlySomeRequiredRoles() = withContext(
        WeComSemanticRules.catalog,
        listOf("root" to wecomClasses()),
        listOf(localDeviceType(), applicationPad()),
        emptyMap(),
    ) { context ->
        assertIs<RecognitionResult.NoMatch>(wecomResult(context))
    }

    private fun qqResult(context: RecognitionContext) = QqSemanticRules.catalog.queries.single().recognize(context)
    private fun wecomResult(context: RecognitionContext) = WeComSemanticRules.catalog.queries.single().recognize(context)

    private fun withContext(
        catalog: SemanticRuleCatalog,
        loaders: List<Pair<String, List<String>>>,
        methods: List<RuleMethod>,
        classes: Map<String, List<RuleClass>>,
        block: (RecognitionContext) -> Unit,
    ) = withCodeDirectory { root ->
        withContextFromRoot(root, catalog, loaders, methods, classes, block)
    }

    private fun withContextResult(
        catalog: SemanticRuleCatalog,
        loaders: List<Pair<String, List<String>>>,
        methods: List<RuleMethod>,
        classes: Map<String, List<RuleClass>>,
        block: (RecognitionContext) -> RecognitionResult,
    ): RecognitionResult {
        var result: RecognitionResult? = null
        withCodeDirectory { root ->
            withContextFromRoot(root, catalog, loaders, methods, classes) { result = block(it) }
        }
        return requireNotNull(result)
    }

    private fun withContextFromRoot(
        root: File,
        catalog: SemanticRuleCatalog,
        loaders: List<Pair<String, List<String>>>,
        methods: List<RuleMethod>,
        classes: Map<String, List<RuleClass>>,
        block: (RecognitionContext) -> Unit,
    ) {
        val inputs = loaders.mapIndexed { index, (id, names) ->
            CodeLoaderInput(id, loaders.getOrNull(index - 1)?.first,
                if (id == "child") CodeDelegation.DELEGATE_LAST else CodeDelegation.PARENT_FIRST,
                listOf(codeArchiveWithClasses(root, "$id.apk", listOf("classes.dex" to names))))
        }
        StartupCodeSnapshot().capture(loaders.last().first, inputs).use { snapshot ->
            EffectiveCodeRuleSession.open(snapshot) { file ->
                val dexClassNames = snapshot.dexes.single { it.file.name == file.name }.classNames
                object : RuleSessionBackend {
                    override fun findMethods(query: MethodQuery): List<RuleMethod> = methods.filter { method ->
                        method.className in dexClassNames &&
                            (query.declaredClass == null || method.className == query.declaredClass) &&
                            (query.parameterTypes == null || method.parameterTypes == query.parameterTypes) &&
                            (query.returnType == null || method.returnType == query.returnType) &&
                            (!query.staticOnly || method.isStatic) && method.strings.containsAll(query.requiredStrings)
                    }
                    override fun findClasses(typeName: String): List<RuleClass> =
                        if (typeName in dexClassNames) classes[typeName].orEmpty() else emptyList()
                    override fun close() = Unit
                }
            }.use { session -> block(RecognitionContext(session, snapshot, catalog)) }
        }
    }

    private fun qqMethod(className: String = "fixture.QqProbe") = RuleMethod(
        "L${className.replace('.', '/')};->deviceType(Landroid/content/Context;)Lfixture/DeviceType;",
        className, listOf("android.content.Context"), "fixture.DeviceType", true, 4,
        setOf("PadUtil", "context is null"),
        listOf(
            RuleFieldReference(className, "deviceType", "fixture.DeviceType", "", true),
            RuleFieldReference(className, "enabled", "java.util.concurrent.atomic.AtomicBoolean", "", true),
        ),
        listOf(RuleMethodReference(className, "initialize", listOf("android.content.Context"), "void")),
    )

    private fun tabletEnum() = RuleClass("fixture.DeviceType", "java.lang.Enum", listOf(
        RuleClassField("TABLET", 0x0008 or 0x0010 or 0x4000),
    ))

    private fun wecomClasses() = listOf(
        "com.tencent.wework.foundation.logic.Application",
        "com.tencent.wework.foundation.logic.IWeworkServiceApi",
        "com.tencent.wework.foundation.impl.WeworkServiceImpl",
    )

    private fun localDeviceType(name: String = "getDeviceTypeFromLocal") = RuleMethod(
        "Lcom/tencent/wework/foundation/logic/Application;->$name()I",
        "com.tencent.wework.foundation.logic.Application", emptyList(), "int", false, 3, emptySet(), emptyList(),
        listOf(RuleMethodReference("com.tencent.wework.foundation.logic.Application", "nativeGetDeviceTypeFromLocal", emptyList(), "int")),
    )

    private fun applicationPad() = RuleMethod(
        "Lcom/tencent/wework/foundation/logic/Application;->isAndroidPad()Z",
        "com.tencent.wework.foundation.logic.Application", emptyList(), "boolean", true, 4, emptySet(),
        listOf(RuleFieldReference("com.tencent.wework.foundation.logic.Application", "mApplicationApi",
            "com.tencent.wework.foundation.logic.IWeworkServiceApi", "", true)),
        listOf(RuleMethodReference("com.tencent.wework.foundation.logic.IWeworkServiceApi", "isAndroidPad", emptyList(), "boolean")),
    )

    private fun servicePad() = RuleMethod(
        "Lcom/tencent/wework/foundation/impl/WeworkServiceImpl;->isAndroidPad()Z",
        "com.tencent.wework.foundation.impl.WeworkServiceImpl", emptyList(), "boolean", false, 10, emptySet(), emptyList(),
        listOf(
            RuleMethodReference("fixture.Provider", "get", emptyList(), "fixture.Provider"),
            RuleMethodReference("fixture.Provider", "isPad", emptyList(), "boolean"),
        ),
    )
}
