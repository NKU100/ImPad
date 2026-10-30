package io.github.nku100.impad.rules.wechat

import com.android.tools.smali.dexlib2.Opcodes
import com.android.tools.smali.dexlib2.dexbacked.DexBackedDexFile
import com.android.tools.smali.dexlib2.iface.Method
import com.android.tools.smali.dexlib2.iface.instruction.*
import com.android.tools.smali.dexlib2.iface.reference.*
import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.compat.rules.VersionRouter
import io.github.nku100.impad.rules.*
import java.io.File
import java.util.zip.ZipFile
import kotlin.test.*

class WeChatTabletLoginRule003Test {
    @Test
    fun unpatchedOfficial3200KeepsItsAcceptedRule002Route() {
        assertEquals(listOf("wechat_tablet_login_002"),
            VersionRouter.candidates(3200, RuleCatalog.forApp("wechat").bindings))
    }

    @Test
    fun activeTinkerCatalogUses003From3200WithoutChangingOlderRoutes() {
        val bindings = WeChatTabletLoginRule003.tinkerCatalog.bindings
        assertEquals(listOf("wechat_tablet_login_001"), VersionRouter.candidates(3040, bindings))
        assertEquals(listOf("wechat_tablet_login_002"), VersionRouter.candidates(3182, bindings))
        assertEquals(listOf("wechat_tablet_login_003"), VersionRouter.candidates(3200, bindings))
    }

    @Test
    fun originalAndPatchedDexBothResolveToTheWrapperAndLinkedLogin() {
        for (patched in listOf(false, true)) {
            withFixture(WeChatForwardRuleDexFixture.Options(
                reloadCachedResult = patched, cachedWrapper = patched,
            )) { session ->
                val result = assertIs<RuleResolution.Unique>(WeChatTabletLoginRule003.resolve(session))
                assertEquals("wechat_tablet_login_003", result.plan.rule.ruleId)
                assertEquals("wechat_tablet_login_behavior_003", result.plan.rule.strategyId)
                assertEquals(mapOf("tablet" to 1, "login" to 1), result.candidateCounts)
                assertEquals("Lfixture/TabletDecision;->isTablet()Z", result.plan.targets.getValue("tablet").descriptor)
                assertEquals("Lfixture/LoginSetup;->update(Landroid/view/View;Landroidx/lifecycle/LifecycleOwner;)V",
                    result.plan.targets.getValue("login").descriptor)
            }
        }
    }

    @Test
    fun brokenWrapperOrUnlinkedLoginCannotProduceAnInstallablePlan() {
        for (options in listOf(
            WeChatForwardRuleDexFixture.Options(reloadCachedResult = true, cachedWrapper = true, wrapperForwardsResult = false),
            WeChatForwardRuleDexFixture.Options(reloadCachedResult = true, cachedWrapper = true, loginCallsTabletHelper = false),
        )) withFixture(options) { session ->
            assertIs<RuleResolution.Missing>(WeChatTabletLoginRule003.resolve(session))
        }
    }

    private fun withFixture(options: WeChatForwardRuleDexFixture.Options, block: (RuleSession) -> Unit) {
        val apk = WeChatForwardRuleDexFixture.apk(options)
        try {
            FixtureDexSession(apk).use(block)
        } finally {
            apk.delete()
        }
    }
}

// Keep the rule integration fixture portable; native DexKit matching is exercised on Android and Linux.
internal class FixtureDexSession(apk: File) : RuleSession {
    private val dex = ZipFile(apk).use { zip ->
        zip.getInputStream(zip.getEntry("classes.dex")).buffered().use {
            DexBackedDexFile.fromInputStream(Opcodes.getDefault(), it)
        }
    }
    private val methods = dex.classes.flatMap { it.methods }.associateBy { it.toString() }

    override fun findMethods(query: MethodQuery): List<RuleMethod> = methods.values.map(::metadata).filter {
        it.strings.containsAll(query.requiredStrings) && (!query.staticOnly || it.isStatic) &&
            (query.declaredClass == null || query.declaredClass == it.className) &&
            (query.returnType == null || query.returnType == it.returnType) &&
            (query.parameterTypes == null || query.parameterTypes == it.parameterTypes)
    }

    override fun findClasses(typeName: String): List<RuleClass> = dex.classes.filter { type(it.type) == typeName }.map {
        RuleClass(type(it.type), it.superclass?.let(::type), it.fields.map { field -> RuleClassField(field.name, field.accessFlags) })
    }

    override fun readInstructions(method: RuleMethod): List<RuleInstruction> {
        var address = 0
        return methods.getValue(method.descriptor).implementation!!.instructions.map { instruction ->
            val reference = (instruction as? ReferenceInstruction)?.reference
            val registers = when (instruction) {
                is FiveRegisterInstruction -> listOf(instruction.registerC, instruction.registerD, instruction.registerE,
                    instruction.registerF, instruction.registerG).take(instruction.registerCount)
                is RegisterRangeInstruction -> (instruction.startRegister until instruction.startRegister + instruction.registerCount).toList()
                else -> emptyList()
            }
            RuleInstruction(
                address, instruction.opcode.name.uppercase().replace('-', '_').replace('/', '_'),
                registerA = (instruction as? OneRegisterInstruction)?.registerA,
                registerB = (instruction as? TwoRegisterInstruction)?.registerB,
                invokeRegisters = registers,
                literal = (instruction as? NarrowLiteralInstruction)?.narrowLiteral,
                branchTarget = (instruction as? OffsetInstruction)?.let { address + it.codeOffset },
                methodReference = (reference as? MethodReference)?.let {
                    RuleInstructionReference(type(it.definingClass), it.name, it.parameterTypes.map { p -> type(p.toString()) }, type(it.returnType))
                },
                typeReference = (reference as? TypeReference)?.type,
                fieldReference = (reference as? FieldReference)?.toString(),
            ).also { address += instruction.codeUnits }
        }
    }

    override fun close() = Unit

    private fun metadata(method: Method): RuleMethod {
        val instructions = method.implementation?.instructions?.toList().orEmpty()
        val references = instructions.mapNotNull { (it as? ReferenceInstruction)?.reference }
        val fields = instructions.mapNotNull { instruction ->
            ((instruction as? ReferenceInstruction)?.reference as? FieldReference)?.let {
                RuleFieldReference(type(it.definingClass), it.name, type(it.type), it.toString(),
                    instruction.opcode.name.startsWith("sget") || instruction.opcode.name.startsWith("iget"))
            }
        }
        return RuleMethod(
            method.toString(), type(method.definingClass), method.parameterTypes.map { type(it.toString()) },
            type(method.returnType), method.accessFlags and 8 != 0, instructions.sumOf { it.codeUnits },
            references.filterIsInstance<StringReference>().map { it.string }.toSet(), fields,
            references.filterIsInstance<MethodReference>().map {
                RuleMethodReference(type(it.definingClass), it.name, it.parameterTypes.map { p -> type(p.toString()) }, type(it.returnType))
            },
        )
    }

    private fun type(descriptor: String): String = when (descriptor) {
        "V" -> "void"
        "Z" -> "boolean"
        "I" -> "int"
        else -> descriptor.removePrefix("L").removeSuffix(";").replace('/', '.')
    }
}
