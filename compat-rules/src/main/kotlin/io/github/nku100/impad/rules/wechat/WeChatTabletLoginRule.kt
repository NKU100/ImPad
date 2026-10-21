package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.compat.rules.MethodShape
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.ResolvedTarget
import io.github.nku100.impad.compat.rules.RuleDefinition
import io.github.nku100.impad.compat.rules.RuleRef
import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.compat.rules.VersionBinding
import io.github.nku100.impad.rules.MethodQuery
import io.github.nku100.impad.rules.RuleCatalog
import io.github.nku100.impad.rules.RuleNumericLiteral
import io.github.nku100.impad.rules.RuleMethod
import io.github.nku100.impad.rules.RuleInstruction
import io.github.nku100.impad.rules.RuleQuery
import io.github.nku100.impad.rules.RuleSession

object WeChatTabletLoginRule : RuleQuery {
    private val TARGET_IDS = setOf("tablet", "login")

    override val definition = RuleDefinition(
        ruleId = RULE_ID,
        strategyId = STRATEGY_ID,
        requiredTargetIds = TARGET_IDS,
    )

    override fun resolve(session: RuleSession): RuleResolution {
        val tablets = session.findMethods(
            MethodQuery(requiredStrings = setOf(LENOVO_MODEL), staticOnly = true, returnType = "boolean"),
        ).filter { isTabletDetector(session, it) }

        val linkedHelpers = tablets.singleOrNull()?.let { tabletHelpers(session, it) }.orEmpty()
        val logins = if (linkedHelpers.size == 1) {
            session.findMethods(
                MethodQuery(requiredStrings = setOf(LOGIN_TRACE_METHOD), staticOnly = true, returnType = "void"),
            ).filter { isLoginEntry(it, linkedHelpers.single()) }
        } else {
            emptyList()
        }

        val matches = linkedMapOf("tablet" to tablets, "login" to logins)
        val counts = matches.mapValues { it.value.size }
        if (matches.values.any { it.size > 1 }) {
            return RuleResolution.Ambiguous(counts, listOf("Multiple DEX definitions matched a WeChat tablet/login target"))
        }
        if (matches.values.any { it.isEmpty() }) {
            return RuleResolution.Missing(
                counts,
                listOf("A required WeChat tablet detector or linked login entry was missing"),
            )
        }

        return RuleResolution.Unique(
            plan = ResolvedPlan(
                rule = RuleRef(RULE_ID, STRATEGY_ID),
                targets = matches.mapValues { (_, methods) -> methods.single().toTarget() },
            ),
            candidateCounts = counts,
        )
    }

    private fun isTabletDetector(session: RuleSession, method: RuleMethod): Boolean {
        if (!method.isStatic || method.returnType != "boolean" || method.parameterTypes.size != 1 ||
            !method.strings.containsAll(TABLET_MODEL_MARKERS)) return false

        val modelReads = method.fields.filter {
            it.className == BUILD && it.name == MODEL_FIELD && it.typeName == "java.lang.String" && it.isRead
        }
        if (modelReads.isEmpty()) return false

        val environmentTypes = classHierarchy(session, method.parameterTypes.single())
        val readsBoxedWidth = method.invokes.any {
            it.className in environmentTypes && it.parameterTypes.isEmpty() && it.returnType == "java.lang.Object"
        }
        val unboxesWidth = method.invokes.any {
            it.className == "java.lang.Integer" && it.name == "intValue" &&
                it.parameterTypes.isEmpty() && it.returnType == "int"
        }
        return readsBoxedWidth && unboxesWidth && hasWidthThresholdReturnShape(session, method, environmentTypes)
    }

    private fun hasWidthThresholdReturnShape(
        session: RuleSession,
        method: RuleMethod,
        environmentTypes: Set<String>,
    ): Boolean {
        val instructions = session.readInstructions(method)
        if (instructions.isEmpty()) return false
        val byAddress = instructions.withIndex().associate { it.value.codeAddress to it.index }
        for (index in 0..instructions.size - WIDTH_COMPARISON_WINDOW) {
            val getter = instructions[index]
            val getterReference = getter.methodReference ?: continue
            if (getter.opcode !in WIDTH_GETTER_OPCODES || getterReference.owner !in environmentTypes ||
                getterReference.parameterTypes.isNotEmpty() || getterReference.returnType != "java.lang.Object") continue

            val resultObject = instructions[index + 1].takeIf { it.opcode == MOVE_RESULT_OBJECT } ?: continue
            val cast = instructions[index + 2].takeIf { it.opcode == CHECK_CAST } ?: continue
            val unbox = instructions[index + 3]
            val unboxReference = unbox.methodReference
            if (unbox.opcode != INVOKE_VIRTUAL || unboxReference?.owner != INTEGER ||
                unboxReference.name != INT_VALUE || unboxReference.parameterTypes.isNotEmpty() ||
                unboxReference.returnType != "int" || unbox.invokeRegisters.firstOrNull() != resultObject.registerA ||
                cast.registerA != resultObject.registerA || cast.typeReference != INTEGER_DESCRIPTOR) continue

            val widthResult = instructions[index + 4].takeIf { it.opcode == MOVE_RESULT } ?: continue
            val threshold = instructions[index + 5]
            if (threshold.opcode != CONST_16 || threshold.literal != WIDTH_THRESHOLD) continue
            val branch = instructions[index + 6]
            if (branch.opcode != WIDTH_BRANCH_OPCODE || branch.registerA != widthResult.registerA ||
                branch.registerB != threshold.registerA) continue

            val targetAddress = branch.branchTarget ?: continue
            val targetIndex = byAddress[targetAddress] ?: continue
            val fallthroughAddress = instructions.getOrNull(index + 7)?.codeAddress ?: continue
            val constantsAtBranch = constantsBefore(instructions, index + 7)
            val falseResult = returnBoolean(instructions, byAddress, targetIndex, constantsAtBranch) ?: continue
            val trueStart = byAddress[fallthroughAddress] ?: continue
            val trueResult = returnBoolean(instructions, byAddress, trueStart, constantsAtBranch) ?: continue
            if (falseResult == 0 && trueResult == 1) return true
        }
        return false
    }

    private fun returnBoolean(
        instructions: List<RuleInstruction>,
        byAddress: Map<Int, Int>,
        startIndex: Int,
        initialConstants: Map<Int, Int>,
    ): Int? {
        val constants = initialConstants.toMutableMap()
        var index = startIndex
        repeat(MAX_LOCAL_PATH_INSTRUCTIONS) {
            val instruction = instructions.getOrNull(index) ?: return null
            val register = instruction.registerA
            when (instruction.opcode) {
                NOP, SPUT_BOOLEAN, SPUT_OBJECT -> Unit
                SGET_BOOLEAN, SGET_OBJECT -> constants.remove(register ?: return null)
                CONST_4, CONST_16, CONST -> {
                    val value = instruction.literal ?: return null
                    if (value !in BOOLEAN_VALUES || register == null) return null
                    constants[register] = value
                }
                MOVE, MOVE_FROM16, MOVE_16 -> {
                    val destination = register ?: return null
                    val source = instruction.registerB ?: return null
                    val value = constants[source]
                    if (value == null) constants.remove(destination) else constants[destination] = value
                }
                CHECK_CAST -> Unit
                GOTO, GOTO_16, GOTO_32 -> {
                    index = byAddress[instruction.branchTarget] ?: return null
                    return@repeat
                }
                RETURN -> return register?.let(constants::get)?.takeIf { it in BOOLEAN_VALUES }
                else -> return null
            }
            index++
        }
        return null
    }

    private fun constantsBefore(instructions: List<RuleInstruction>, endIndex: Int): Map<Int, Int> {
        val constants = mutableMapOf<Int, Int>()
        val invalidRegisters = mutableSetOf<Int>()
        instructions.take(endIndex).forEach { instruction ->
            val register = instruction.registerA
            when (instruction.opcode) {
                CONST_4, CONST_16, CONST -> {
                    if (register != null && register !in invalidRegisters) {
                        val value = instruction.literal?.takeIf { it in BOOLEAN_VALUES }
                        val previous = constants[register]
                        if (value == null || (previous != null && previous != value)) {
                            constants.remove(register)
                            invalidRegisters += register
                        } else {
                            constants[register] = value
                        }
                    }
                }
                MOVE, MOVE_FROM16, MOVE_16 -> {
                    if (register != null && register !in invalidRegisters) {
                        val value = instruction.registerB?.let(constants::get)
                        val previous = constants[register]
                        if (value == null || (previous != null && previous != value)) {
                            constants.remove(register)
                            invalidRegisters += register
                        } else {
                            constants[register] = value
                        }
                    }
                }
                else -> if (register != null && !instruction.opcode.isReadOnlyRegisterOperation()) {
                    constants.remove(register)
                    invalidRegisters += register
                }
            }
        }
        return constants
    }

    private fun String.isReadOnlyRegisterOperation(): Boolean =
        startsWith("IF_") || startsWith("RETURN") || startsWith("SPUT") ||
            startsWith("MONITOR") || this == "THROW"

    private fun classHierarchy(session: RuleSession, typeName: String): Set<String> {
        val hierarchy = linkedSetOf<String>()
        var current: String? = typeName
        while (current != null && current != "java.lang.Object" && hierarchy.add(current)) {
            val definitions = session.findClasses(current)
            if (definitions.size != 1 || definitions.single().className != current) return emptySet()
            current = definitions.single().superClassName
        }
        return hierarchy
    }

    private fun tabletHelpers(session: RuleSession, tablet: RuleMethod): List<RuleMethod> =
        session.findMethods(
            MethodQuery(declaredClass = tablet.className, parameterTypes = emptyList(), staticOnly = true, returnType = "boolean"),
        ).filter { helper ->
            helper.isStatic && helper.parameterTypes.isEmpty() && helper.returnType == "boolean" &&
                helper.invokes.count { it.matches(tablet) } == 1
        }

    private fun isLoginEntry(method: RuleMethod, tabletHelper: RuleMethod): Boolean =
        method.isStatic && method.parameterTypes.size == 2 && method.parameterTypes.first() == VIEW &&
            method.parameterTypes[1] !in PRIMITIVE_TYPES && method.returnType == "void" &&
            method.strings.contains(LOGIN_TRACE_OWNER) && method.invokes.any {
                it.className == VIEW && it.name == SET_VISIBILITY &&
                    it.parameterTypes == listOf("int") && it.returnType == "void"
            } && method.invokes.count { it.matches(tabletHelper) } == 1

    private fun io.github.nku100.impad.rules.RuleMethodReference.matches(method: RuleMethod): Boolean =
        className == method.className && name == method.methodName() &&
            parameterTypes == method.parameterTypes && returnType == method.returnType

    private fun RuleMethod.methodName(): String = descriptor.substringAfter("->", "").substringBefore('(')

    private fun RuleMethod.toTarget() = ResolvedTarget(
        descriptor = descriptor,
        shape = MethodShape(parameterTypes, returnType, isStatic),
    )

    val catalog: RuleCatalog by lazy {
        RuleCatalog(
            appId = "wechat",
            queries = listOf(this),
            bindings = listOf(VersionBinding(ruleId = RULE_ID, min = 3040, max = null)),
            strategyIds = setOf(STRATEGY_ID),
        )
    }

    private const val RULE_ID = "wechat_tablet_login_001"
    private const val STRATEGY_ID = "wechat_tablet_login_behavior_001"
    private const val BUILD = "android.os.Build"
    private const val MODEL_FIELD = "MODEL"
    private const val LENOVO_MODEL = "Lenovo TB-9707F"
    private const val VIEW = "android.view.View"
    private const val SET_VISIBILITY = "setVisibility"
    private const val LOGIN_TRACE_OWNER = "com/tencent/mm/plugin/account/model/LoginUtil\$Companion"
    private const val LOGIN_TRACE_METHOD = "updateLoginAsExdevice"
    private const val INTEGER = "java.lang.Integer"
    private const val INTEGER_DESCRIPTOR = "Ljava/lang/Integer;"
    private const val INT_VALUE = "intValue"
    private const val WIDTH_THRESHOLD = 600
    private const val WIDTH_BRANCH_OPCODE = "IF_LT"
    private const val INVOKE_VIRTUAL = "INVOKE_VIRTUAL"
    private val WIDTH_GETTER_OPCODES = setOf(INVOKE_VIRTUAL, "INVOKE_INTERFACE")
    private const val MOVE_RESULT_OBJECT = "MOVE_RESULT_OBJECT"
    private const val CHECK_CAST = "CHECK_CAST"
    private const val MOVE_RESULT = "MOVE_RESULT"
    private const val CONST_4 = "CONST_4"
    private const val CONST_16 = "CONST_16"
    private const val CONST = "CONST"
    private const val MOVE = "MOVE"
    private const val MOVE_FROM16 = "MOVE_FROM16"
    private const val MOVE_16 = "MOVE_16"
    private const val GOTO = "GOTO"
    private const val GOTO_16 = "GOTO_16"
    private const val GOTO_32 = "GOTO_32"
    private const val NOP = "NOP"
    private const val SGET_BOOLEAN = "SGET_BOOLEAN"
    private const val SGET_OBJECT = "SGET_OBJECT"
    private const val SPUT_BOOLEAN = "SPUT_BOOLEAN"
    private const val SPUT_OBJECT = "SPUT_OBJECT"
    private const val RETURN = "RETURN"
    private const val WIDTH_COMPARISON_WINDOW = 7
    private const val MAX_LOCAL_PATH_INSTRUCTIONS = 12
    private val BOOLEAN_VALUES = setOf(0, 1)
    private val TABLET_MODEL_MARKERS = setOf(LENOVO_MODEL, "X2", "A2")
    private val PRIMITIVE_TYPES = setOf("boolean", "byte", "char", "short", "int", "long", "float", "double")
}
