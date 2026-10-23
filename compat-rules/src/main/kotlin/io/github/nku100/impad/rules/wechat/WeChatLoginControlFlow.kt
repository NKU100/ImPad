package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.rules.RuleInstruction
import io.github.nku100.impad.rules.RuleInstructionReference

internal object WeChatLoginControlFlow {
    fun forwardsBooleanResult(code: List<RuleInstruction>, target: RuleInstructionReference): Boolean {
        val call = code.indexOfFirst { it.opcode == "INVOKE_STATIC" && it.methodReference == target }
        if (call < 0 || code.take(call).any {
                it.opcode.startsWith("IF_") || it.opcode.startsWith("GOTO") ||
                    it.opcode.startsWith("RETURN") || it.opcode.contains("SWITCH") || it.opcode == "THROW"
            }) return false
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

    private fun reachesVisibility(code: List<RuleInstruction>, start: Int): Boolean? {
        val byAddress = code.withIndex().associate { it.value.codeAddress to it.index }
        val pending = ArrayDeque<Int>()
        val visited = mutableSetOf<Int>()
        var found = false
        pending.add(start)
        while (pending.isNotEmpty()) {
            val index = pending.removeLast()
            if (!visited.add(index)) continue
            if (visited.size > 256) return null
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
