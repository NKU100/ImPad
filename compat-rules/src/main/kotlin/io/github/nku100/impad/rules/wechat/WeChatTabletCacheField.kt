package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.compat.rules.TargetCodeSource
import io.github.nku100.impad.rules.RuleInstruction
import io.github.nku100.impad.rules.RuleInstructionReference
import io.github.nku100.impad.rules.RuleMethod
import io.github.nku100.impad.rules.RuleSession
import java.lang.reflect.Modifier

internal data class ResolvedTabletCacheField(val descriptor: String, val source: TargetCodeSource, val decisionDescriptor: String)

internal object WeChatTabletCacheField {
    fun resolve(session: RuleSession, decision: RuleMethod): ResolvedTabletCacheField? {
        val source = session.sourceOf(decision)
        return session.inContextOf(decision).use { view ->
            val owner = decision.descriptor.substringBefore("->")
            val field = extract(owner, view.readInstructions(decision)) ?: return@use null
            val declaration = view.findClasses(decision.className).singleOrNull { it.className == decision.className }
                ?: return@use null
            val name = field.substringAfter("->").substringBefore(':')
            val declaredField = declaration.fields.singleOrNull { it.name == name } ?: return@use null
            if (declaredField.className != decision.className || declaredField.typeName != "java.lang.Boolean" || !Modifier.isStatic(declaredField.accessFlags) ||
                Modifier.isFinal(declaredField.accessFlags)) return@use null
            ResolvedTabletCacheField(field, source, decision.descriptor)
        }
    }

    // Extracts a field relationship from an already recognized decision; binding must validate its declaration and source.
    fun extract(declaringType: String, instructions: List<RuleInstruction>): String? {
        if (instructions.isEmpty() || instructions.first().codeAddress < 0 ||
            instructions.zipWithNext().any { (left, right) -> left.codeAddress >= right.codeAddress }) return null
        val byAddress = instructions.withIndex().associate { it.value.codeAddress to it.index }
        val candidates = mutableSetOf<String>()
        for (index in instructions.indices) {
            val read = instructions[index]
            val field = read.fieldReference ?: continue
            val name = field.removePrefix("$declaringType->").removeSuffix(BOOLEAN_SUFFIX)
            if (!field.startsWith("$declaringType->") || !field.endsWith(BOOLEAN_SUFFIX) ||
                name.isEmpty() || name.any { it in ";:()/.[" } ||
                read.opcode != "SGET_OBJECT" || read.registerA == null) continue
            val branch = instructions.getOrNull(index + 1) ?: continue
            if (branch.opcode != "IF_EQZ" || branch.registerA != read.registerA) continue
            var unboxIndex = index + 2
            var receiver = read.registerA
            val reload = instructions.getOrNull(unboxIndex)
            if (reload?.opcode == "SGET_OBJECT") {
                if (reload.fieldReference != field || reload.registerA == null) continue
                receiver = reload.registerA
                unboxIndex++
            }
            val unbox = instructions.getOrNull(unboxIndex) ?: continue
            val result = instructions.getOrNull(unboxIndex + 1) ?: continue
            val returned = instructions.getOrNull(unboxIndex + 2) ?: continue
            if (unbox.opcode != "INVOKE_VIRTUAL" || unbox.invokeRegisters != listOf(receiver) ||
                unbox.methodReference != BOOLEAN_VALUE || result.opcode != "MOVE_RESULT" || result.registerA == null ||
                returned.opcode != "RETURN" || returned.registerA != result.registerA) continue
            val coldIndex = byAddress[branch.branchTarget] ?: continue
            if (instructions[coldIndex].codeAddress <= returned.codeAddress) continue
            if (hasReachableWrite(instructions, byAddress, coldIndex, field)) candidates += field
        }
        return candidates.singleOrNull()
    }

    private fun hasReachableWrite(
        instructions: List<RuleInstruction>, byAddress: Map<Int, Int>, start: Int, field: String,
    ): Boolean {
        val pending = ArrayDeque<Int>()
        val visited = mutableSetOf<Int>()
        var found = false
        pending.add(start)
        while (pending.isNotEmpty()) {
            val index = pending.removeFirst()
            if (!visited.add(index)) continue
            val instruction = instructions.getOrNull(index) ?: return false
            if (instruction.opcode == "SPUT_OBJECT" && instruction.fieldReference == field && instruction.registerA != null) found = true
            when {
                instruction.opcode.startsWith("RETURN") || instruction.opcode == "THROW" -> Unit
                instruction.opcode.startsWith("GOTO") -> {
                    pending.add(byAddress[instruction.branchTarget] ?: return false)
                }
                instruction.opcode.startsWith("IF_") -> {
                    pending.add(byAddress[instruction.branchTarget] ?: return false)
                    pending.add(index + 1)
                }
                instruction.opcode.contains("SWITCH") || instruction.opcode.endsWith("PAYLOAD") -> return false
                else -> pending.add(index + 1)
            }
        }
        return found
    }

    private const val BOOLEAN_SUFFIX = ":Ljava/lang/Boolean;"
    private val BOOLEAN_VALUE = RuleInstructionReference("java.lang.Boolean", "booleanValue", emptyList(), "boolean")
}
