package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.compat.rules.MethodShape
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.ResolvedTarget
import io.github.nku100.impad.compat.rules.RuleDefinition
import io.github.nku100.impad.compat.rules.RuleRef
import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.rules.MethodQuery
import io.github.nku100.impad.rules.RuleInstruction
import io.github.nku100.impad.rules.RuleInstructionReference
import io.github.nku100.impad.rules.RuleMethod
import io.github.nku100.impad.rules.RuleMethodReference
import io.github.nku100.impad.rules.RuleQuery
import io.github.nku100.impad.rules.RuleSession

/** The newer aggregate tablet check and its linked alternate-device login setup. */
object WeChatTabletLoginRule002 : RuleQuery {
    private val targetIds = setOf("tablet", "login")

    override val definition = RuleDefinition(
        ruleId = RULE_ID,
        strategyId = STRATEGY_ID,
        requiredTargetIds = targetIds,
    )

    override fun resolve(session: RuleSession): RuleResolution {
        val modelGates = session.findMethods(
            MethodQuery(requiredStrings = setOf(LENOVO_MODEL), staticOnly = true, returnType = "boolean"),
        ).filter(::isModelGate)
        val tablets = session.findMethods(
            MethodQuery(requiredStrings = TABLET_MODEL_MARKERS, staticOnly = true, returnType = "boolean"),
        ).filter { isTabletDecision(session, it, modelGates) }

        val helpers = tablets.singleOrNull()?.let { tabletHelpers(session, it) }.orEmpty()
        val logins = if (helpers.size == 1) {
            session.findMethods(
                MethodQuery(requiredStrings = setOf(LOGIN_TRACE_METHOD), staticOnly = true, returnType = "void"),
            ).filter { isLoginEntry(session, it, helpers.single()) }
        } else {
            emptyList()
        }

        val matches = linkedMapOf("tablet" to tablets, "login" to logins)
        val counts = matches.mapValues { it.value.size }
        if (matches.values.any { it.size > 1 }) {
            return RuleResolution.Ambiguous(
                counts,
                listOf("Multiple DEX definitions matched the newer WeChat tablet/login path"),
            )
        }
        if (matches.values.any { it.isEmpty() }) {
            return RuleResolution.Missing(
                counts,
                listOf("A required WeChat aggregate tablet decision or linked alternate-device entry was missing"),
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

    private fun isModelGate(method: RuleMethod): Boolean =
        method.isStatic && method.parameterTypes.isEmpty() && method.returnType == "boolean" &&
            method.strings.contains(LENOVO_MODEL) && hasBuildModelRead(method) &&
            hasBooleanCache(method) && method.invokes.any {
                it.className == STRING && it.name == EQUALS_IGNORE_CASE &&
                    it.parameterTypes == listOf(STRING) && it.returnType == "boolean"
            }

    private fun isTabletDecision(
        session: RuleSession,
        method: RuleMethod,
        modelGates: List<RuleMethod>,
    ): Boolean {
        if (!method.isStatic || method.returnType != "boolean" || method.parameterTypes.size != 1 ||
            method.parameterTypes.single() in PRIMITIVE_TYPES ||
            !method.strings.containsAll(TABLET_MODEL_MARKERS) ||
            !hasBuildModelRead(method) || !hasBooleanCache(method) ||
            method.invokes.count {
                it.className == STRING && it.name == EQUALS_IGNORE_CASE &&
                    it.parameterTypes == listOf(STRING) && it.returnType == "boolean"
            } < 2) return false

        val linkedModelGates = modelGates.filter { gate ->
            method.invokes.count { it.matches(gate) } == 1
        }
        if (linkedModelGates.size != 1) return false

        val instructions = session.readInstructions(method)
        if (instructions.isEmpty() || !hasCachedBooleanEarlyReturn(instructions)) return false
        val hierarchy = classHierarchy(session, method.parameterTypes.single())
        if (hierarchy.isEmpty()) return false

        return hasModelGateSuccessPath(instructions, linkedModelGates.single()) &&
            hasWidthThresholdReturnShape(instructions, hierarchy)
    }

    private fun hasCachedBooleanEarlyReturn(instructions: List<RuleInstruction>): Boolean {
        for (index in 0..instructions.size - CACHED_RETURN_WINDOW) {
            val cacheRead = instructions[index].takeIf {
                it.opcode == SGET_OBJECT && it.registerA != null
            } ?: continue
            val cacheRegister = cacheRead.registerA ?: continue
            val branch = instructions[index + 1].takeIf {
                it.opcode == IF_EQZ && it.registerA == cacheRegister
            } ?: continue
            val booleanValue = instructions[index + 2]
            val reference = booleanValue.methodReference
            if (booleanValue.opcode != INVOKE_VIRTUAL || reference?.owner != BOOLEAN ||
                reference.name != BOOLEAN_VALUE || reference.parameterTypes.isNotEmpty() ||
                reference.returnType != "boolean" || booleanValue.invokeRegisters.firstOrNull() != cacheRegister) continue
            val result = instructions[index + 3].takeIf { it.opcode == MOVE_RESULT } ?: continue
            if (instructions[index + 4].opcode == RETURN && instructions[index + 4].registerA == result.registerA &&
                branch.branchTarget != null) return true
        }
        return false
    }

    private fun hasModelGateSuccessPath(
        instructions: List<RuleInstruction>,
        modelGate: RuleMethod,
    ): Boolean {
        val byAddress = instructions.withIndex().associate { it.value.codeAddress to it.index }
        for (index in instructions.indices) {
            val call = instructions[index]
            if (call.opcode != INVOKE_STATIC || call.methodReference?.matches(modelGate) != true) continue
            val result = instructions.getOrNull(index + 1)?.takeIf { it.opcode == MOVE_RESULT } ?: continue
            val branchIndex = index + 2
            val branch = instructions.getOrNull(branchIndex) ?: continue
            if (branch.registerA != result.registerA || branch.opcode !in setOf(IF_EQZ, IF_NEZ)) continue
            val constants = constantsBefore(instructions, branchIndex)
            val successStart = if (branch.opcode == IF_EQZ) {
                branchIndex + 1
            } else {
                byAddress[branch.branchTarget] ?: continue
            }
            if (returnBooleanPath(instructions, byAddress, successStart, constants) == 1) return true
        }
        return false
    }

    private fun hasWidthThresholdReturnShape(
        instructions: List<RuleInstruction>,
        environmentTypes: Set<String>,
    ): Boolean {
        val byAddress = instructions.withIndex().associate { it.value.codeAddress to it.index }
        for (index in 0..instructions.size - WIDTH_COMPARISON_WINDOW) {
            val getter = instructions[index]
            val getterReference = getter.methodReference ?: continue
            if (getter.opcode !in WIDTH_GETTER_OPCODES || getterReference.owner !in environmentTypes ||
                getterReference.parameterTypes.isNotEmpty() || getterReference.returnType != OBJECT) continue

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
            val branchIndex = index + 6
            val branch = instructions[branchIndex]
            if (branch.opcode != WIDTH_BRANCH_OPCODE || branch.registerA != widthResult.registerA ||
                branch.registerB != threshold.registerA) continue

            val targetIndex = branch.branchTarget?.let(byAddress::get) ?: continue
            val constants = constantsBefore(instructions, branchIndex)
            val falseResult = returnBooleanPath(instructions, byAddress, targetIndex, constants) ?: continue
            val trueResult = returnBooleanPath(instructions, byAddress, branchIndex + 1, constants) ?: continue
            if (falseResult == 0 && trueResult == 1) return true
        }
        return false
    }

    private fun returnBooleanPath(
        instructions: List<RuleInstruction>,
        byAddress: Map<Int, Int>,
        startIndex: Int,
        initialConstants: Map<Int, Int>,
    ): Int? {
        val constants = initialConstants.toMutableMap()
        val visited = mutableSetOf<Int>()
        var index = startIndex
        repeat(MAX_LOCAL_PATH_INSTRUCTIONS) {
            if (!visited.add(index)) return null
            val instruction = instructions.getOrNull(index) ?: return null
            val register = instruction.registerA
            when (instruction.opcode) {
                NOP, CHECK_CAST, SPUT_BOOLEAN, SPUT_OBJECT, IPUT_OBJECT, IPUT_BOOLEAN,
                APUT_OBJECT, APUT_BOOLEAN, MONITOR_ENTER, MONITOR_EXIT, THROW -> Unit
                CONST_4, CONST_16, CONST -> {
                    if (register != null) {
                        val value = instruction.literal?.takeIf { it in BOOLEAN_VALUES }
                        if (value == null) constants.remove(register) else constants[register] = value
                    }
                }
                MOVE, MOVE_FROM16, MOVE_16 -> {
                    val destination = register ?: return null
                    val value = instruction.registerB?.let(constants::get)
                    if (value == null) constants.remove(destination) else constants[destination] = value
                }
                SGET_BOOLEAN, SGET_OBJECT, MOVE_RESULT, MOVE_RESULT_OBJECT, MOVE_RESULT_WIDE,
                CONST_STRING, CONST_STRING_JUMBO, CONST_CLASS, NEW_INSTANCE, MOVE_EXCEPTION,
                INSTANCE_OF, ARRAY_LENGTH -> constants.remove(register ?: return null)
                GOTO, GOTO_16, GOTO_32 -> {
                    index = byAddress[instruction.branchTarget] ?: return null
                    return@repeat
                }
                RETURN -> return register?.let(constants::get)?.takeIf { it in BOOLEAN_VALUES }
                else -> {
                    if (instruction.opcode.startsWith("INVOKE_") || instruction.opcode.startsWith("FILLED_NEW_ARRAY")) {
                        // Calls do not write a destination register until a following move-result.
                    } else if (instruction.opcode.startsWith("IF_")) {
                        return null
                    } else if (register != null && !instruction.opcode.isReadOnlyRegisterOperation()) {
                        constants.remove(register)
                    } else {
                        return null
                    }
                }
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

    private fun hasBuildModelRead(method: RuleMethod): Boolean = method.fields.any {
        it.className == BUILD && it.name == MODEL_FIELD && it.typeName == STRING && it.isRead
    }

    private fun hasBooleanCache(method: RuleMethod): Boolean = method.fields
        .filter { it.typeName == BOOLEAN }
        .groupBy { it.className to it.name }
        .values
        .any { fields -> fields.any { it.isRead } && fields.any { !it.isRead } }

    private fun classHierarchy(session: RuleSession, typeName: String): Set<String> {
        val hierarchy = linkedSetOf<String>()
        var current: String? = typeName
        while (current != null && current != JAVA_OBJECT && hierarchy.add(current)) {
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
                helper.invokes.count { it.matches(tablet) } == 1 &&
                WeChatLoginControlFlow.forwardsBooleanResult(session.readInstructions(helper), tablet.reference())
        }

    private fun isLoginEntry(session: RuleSession, method: RuleMethod, tabletHelper: RuleMethod): Boolean =
        method.isStatic && method.parameterTypes.size == 2 && method.parameterTypes.first() == VIEW &&
            method.parameterTypes[1] !in PRIMITIVE_TYPES && method.returnType == "void" &&
            method.strings.contains(LOGIN_TRACE_OWNER) && method.invokes.any {
                it.className == VIEW && it.name == SET_VISIBILITY &&
                    it.parameterTypes == listOf("int") && it.returnType == "void"
            } && method.invokes.count { it.matches(tabletHelper) } == 1 &&
            WeChatLoginControlFlow.controlsVisibility(session.readInstructions(method), tabletHelper.reference())

    private fun RuleMethod.reference() = RuleInstructionReference(className, methodName(), parameterTypes, returnType)

    private fun RuleMethodReference.matches(method: RuleMethod): Boolean =
        className == method.className && name == method.methodName() &&
            parameterTypes == method.parameterTypes && returnType == method.returnType

    private fun RuleInstructionReference.matches(method: RuleMethod): Boolean =
        owner == method.className && name == method.methodName() &&
            parameterTypes == method.parameterTypes && returnType == method.returnType

    private fun RuleMethod.methodName(): String = descriptor.substringAfter("->", "").substringBefore('(')

    private fun String.isReadOnlyRegisterOperation(): Boolean =
        startsWith("IF_") || startsWith("RETURN") || startsWith("SPUT") ||
            startsWith("MONITOR") || this == THROW

    private fun RuleMethod.toTarget() = ResolvedTarget(
        descriptor = descriptor,
        shape = MethodShape(parameterTypes, returnType, isStatic),
    )

    private val PRIMITIVE_TYPES = setOf("boolean", "byte", "char", "short", "int", "long", "float", "double")
    private val TABLET_MODEL_MARKERS = setOf("X2", "A2")
    private val WIDTH_GETTER_OPCODES = setOf(INVOKE_VIRTUAL, "INVOKE_INTERFACE")

    private const val RULE_ID = "wechat_tablet_login_002"
    private const val STRATEGY_ID = "wechat_tablet_login_behavior_002"
    private const val BUILD = "android.os.Build"
    private const val MODEL_FIELD = "MODEL"
    private const val LENOVO_MODEL = "Lenovo TB-9707F"
    private const val VIEW = "android.view.View"
    private const val SET_VISIBILITY = "setVisibility"
    private const val LOGIN_TRACE_OWNER = "com/tencent/mm/plugin/account/model/LoginUtil\$Companion"
    private const val LOGIN_TRACE_METHOD = "updateLoginAsExdevice"
    private const val STRING = "java.lang.String"
    private const val BOOLEAN = "java.lang.Boolean"
    private const val BOOLEAN_VALUE = "booleanValue"
    private const val EQUALS_IGNORE_CASE = "equalsIgnoreCase"
    private const val INTEGER = "java.lang.Integer"
    private const val INTEGER_DESCRIPTOR = "Ljava/lang/Integer;"
    private const val INT_VALUE = "intValue"
    private const val OBJECT = "java.lang.Object"
    private const val JAVA_OBJECT = "java.lang.Object"
    private const val WIDTH_THRESHOLD = 600
    private const val WIDTH_BRANCH_OPCODE = "IF_LT"
    private const val INVOKE_STATIC = "INVOKE_STATIC"
    private const val MOVE_RESULT_OBJECT = "MOVE_RESULT_OBJECT"
    private const val MOVE_RESULT = "MOVE_RESULT"
    private const val MOVE_RESULT_WIDE = "MOVE_RESULT_WIDE"
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
    private const val IPUT_OBJECT = "IPUT_OBJECT"
    private const val IPUT_BOOLEAN = "IPUT_BOOLEAN"
    private const val APUT_OBJECT = "APUT_OBJECT"
    private const val APUT_BOOLEAN = "APUT_BOOLEAN"
    private const val INVOKE_VIRTUAL = "INVOKE_VIRTUAL"
    private const val CHECK_CAST = "CHECK_CAST"
    private const val IF_EQZ = "IF_EQZ"
    private const val IF_NEZ = "IF_NEZ"
    private const val RETURN = "RETURN"
    private const val MONITOR_ENTER = "MONITOR_ENTER"
    private const val MONITOR_EXIT = "MONITOR_EXIT"
    private const val THROW = "THROW"
    private const val CONST_STRING = "CONST_STRING"
    private const val CONST_STRING_JUMBO = "CONST_STRING_JUMBO"
    private const val CONST_CLASS = "CONST_CLASS"
    private const val NEW_INSTANCE = "NEW_INSTANCE"
    private const val MOVE_EXCEPTION = "MOVE_EXCEPTION"
    private const val INSTANCE_OF = "INSTANCE_OF"
    private const val ARRAY_LENGTH = "ARRAY_LENGTH"
    private const val WIDTH_COMPARISON_WINDOW = 7
    private const val CACHED_RETURN_WINDOW = 5
    private const val MAX_LOCAL_PATH_INSTRUCTIONS = 64
    private val BOOLEAN_VALUES = setOf(0, 1)
}
