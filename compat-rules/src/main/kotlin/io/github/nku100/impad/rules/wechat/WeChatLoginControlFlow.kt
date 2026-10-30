package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.rules.AnalysisIncompleteException
import io.github.nku100.impad.rules.RuleInstruction
import io.github.nku100.impad.rules.RuleInstructionReference

internal object WeChatLoginControlFlow {
    fun forwardsCachedBooleanResult(
        code: List<RuleInstruction>, target: RuleInstructionReference, cacheField: String,
    ): Boolean {
        val coldCall = code.indexOfFirst { it.opcode == "INVOKE_STATIC" && it.methodReference == target }
        if (coldCall < 0) return false
        val coldReturn = booleanReturn(code, coldCall) ?: return false
        for (index in 0 until code.size - 2) {
            val read = code[index]
            val branch = code[index + 1]
            val unbox = code[index + 2]
            if (read.opcode != "SGET_OBJECT" || read.fieldReference != cacheField || read.registerA == null ||
                branch.opcode != "IF_EQZ" || branch.registerA != read.registerA ||
                unbox.opcode != "INVOKE_VIRTUAL" || unbox.invokeRegisters != listOf(read.registerA) ||
                unbox.methodReference != RuleInstructionReference("java.lang.Boolean", "booleanValue", emptyList(), "boolean")) continue
            if (!entersCacheOrForcesTrue(code, index, coldReturn)) continue
            val coldStart = code.indexOfFirst { it.codeAddress == branch.branchTarget }
            if (coldStart <= index + 2 || coldStart > coldCall) continue
            val preparation = code.subList(coldStart, coldCall)
            if (preparation.none { it.opcode == "MONITOR_ENTER" } ||
                preparation.any { it.opcode !in setOf("SGET_OBJECT", "MONITOR_ENTER", "NOP") }) continue
            if (booleanReturn(code, index + 2) == coldReturn) return true
        }
        return false
    }

    private fun entersCacheOrForcesTrue(code: List<RuleInstruction>, cacheEntry: Int, returnIndex: Int): Boolean {
        val prefix = code.take(cacheEntry)
        if (prefix.none { it.opcode.isControlTransfer() }) return true
        if (prefix.size < 4 || prefix.dropLast(4).any { it.opcode.isControlTransfer() }) return false
        val (flag, branch, constant, jump) = prefix.takeLast(4)
        return flag.opcode == "SGET_BOOLEAN" && flag.registerA != null && flag.fieldReference?.endsWith(":Z") == true &&
            branch.opcode == "IF_EQZ" && branch.registerA == flag.registerA && branch.branchTarget == code[cacheEntry].codeAddress &&
            constant.opcode in setOf("CONST_4", "CONST_16", "CONST") && constant.literal == 1 &&
            constant.registerA != null && constant.registerA == code[returnIndex].registerA &&
            jump.opcode in setOf("GOTO", "GOTO_16", "GOTO_32") && jump.branchTarget == code[returnIndex].codeAddress
    }

    private fun booleanReturn(code: List<RuleInstruction>, call: Int): Int? {
        val result = code.getOrNull(call + 1)?.takeIf { it.opcode == "MOVE_RESULT" } ?: return null
        val aliases = mutableSetOf(result.registerA ?: return null)
        val visited = mutableSetOf<Int>()
        var index = call + 2
        while (visited.add(index)) {
            if (visited.size > 64) throw AnalysisIncompleteException("Cached wrapper return traversal exceeded 64 instructions")
            val instruction = code.getOrNull(index) ?: return null
            when (instruction.opcode) {
                "NOP", "MONITOR_EXIT" -> Unit
                "MOVE", "MOVE_FROM16", "MOVE_16" -> {
                    val destination = instruction.registerA ?: return null
                    if (instruction.registerB in aliases) aliases += destination else aliases -= destination
                }
                "GOTO", "GOTO_16", "GOTO_32" -> {
                    index = code.indexOfFirst { it.codeAddress == instruction.branchTarget }
                    if (index < 0) return null
                    continue
                }
                "RETURN" -> return index.takeIf { instruction.registerA in aliases }
                else -> return null
            }
            index++
        }
        return null
    }

    fun forwardsBooleanResult(code: List<RuleInstruction>, target: RuleInstructionReference): Boolean {
        val call = code.indexOfFirst { it.opcode == "INVOKE_STATIC" && it.methodReference == target }
        if (call < 0 || code.take(call).any { it.opcode.isControlTransfer() }) return false
        val result = forwardedResult(code, call) ?: return false
        val end = code.getOrNull(result.first) ?: return false
        return end.opcode == "RETURN" && end.registerA in result.second
    }

    fun controlsVisibility(code: List<RuleInstruction>, target: RuleInstructionReference): Boolean {
        val call = code.indexOfFirst { it.opcode == "INVOKE_STATIC" && it.methodReference == target }
        if (call < 0) return false
        val result = forwardedResult(code, call) ?: return false
        val branch = code.getOrNull(result.first) ?: return false
        if (branch.opcode !in setOf("IF_EQZ", "IF_NEZ") || branch.registerA !in result.second) return false
        val targetIndex = code.indexOfFirst { it.codeAddress == branch.branchTarget }
        if (targetIndex < 0) return false
        val falseIndex = if (branch.opcode == "IF_EQZ") targetIndex else result.first + 1
        val trueIndex = if (branch.opcode == "IF_NEZ") targetIndex else result.first + 1
        return reachesVisibility(code, falseIndex) == true && reachesVisibility(code, trueIndex) == false
    }

    private fun forwardedResult(code: List<RuleInstruction>, call: Int): Pair<Int, Set<Int>>? {
        val result = code.getOrNull(call + 1)?.takeIf { it.opcode == "MOVE_RESULT" } ?: return null
        val aliases = mutableSetOf(result.registerA ?: return null)
        var index = call + 2
        while (index < code.size) {
            val instruction = code[index]
            when (instruction.opcode) {
                "NOP" -> Unit
                "MOVE", "MOVE_FROM16", "MOVE_16" -> {
                    val destination = instruction.registerA ?: return null
                    if (instruction.registerB in aliases) aliases += destination else aliases -= destination
                }
                else -> return index to aliases
            }
            index++
        }
        return null
    }

    private fun String.isControlTransfer(): Boolean = startsWith("IF_") || startsWith("GOTO") ||
        startsWith("RETURN") || contains("SWITCH") || this == "THROW"

    private fun reachesVisibility(code: List<RuleInstruction>, start: Int): Boolean? {
        val byAddress = code.withIndex().associate { it.value.codeAddress to it.index }
        val pending = ArrayDeque<Int>()
        val visited = mutableSetOf<Int>()
        var found = false
        pending.add(start)
        while (pending.isNotEmpty()) {
            val index = pending.removeLast()
            if (!visited.add(index)) continue
            if (visited.size > 256) throw AnalysisIncompleteException("Login visibility traversal exceeded 256 instructions")
            val instruction = code.getOrNull(index) ?: return null
            val method = instruction.methodReference
            if (instruction.opcode.startsWith("INVOKE_VIRTUAL") && method?.owner == "android.view.View" &&
                method.name == "setVisibility" && method.parameterTypes == listOf("int") &&
                method.returnType == "void") found = true
            when {
                instruction.opcode.startsWith("RETURN") || instruction.opcode == "THROW" -> Unit
                instruction.opcode.startsWith("GOTO") ->
                    pending.add(byAddress[instruction.branchTarget] ?: return null)
                instruction.opcode.startsWith("IF_") -> {
                    pending.add(byAddress[instruction.branchTarget] ?: return null)
                    pending.add(index + 1)
                }
                instruction.opcode.contains("SWITCH") -> return null
                else -> pending.add(index + 1)
            }
        }
        return found
    }
}
