package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.rules.RuleInstruction
import io.github.nku100.impad.rules.RuleInstructionReference

internal object WeChatCachedBooleanReturn {
    fun matches(instructions: List<RuleInstruction>): Boolean = instructions.windowed(6).any { window ->
        val (read, branch, reload, unbox, result) = window
        val returned = window[5]
        val field = read.fieldReference
        read.opcode == "SGET_OBJECT" && read.registerA != null &&
            field?.endsWith(":Ljava/lang/Boolean;") == true &&
            branch.opcode == "IF_EQZ" && branch.registerA == read.registerA &&
            branch.branchTarget?.let { target ->
                target > returned.codeAddress && instructions.any { it.codeAddress == target }
            } == true &&
            reload.opcode == "SGET_OBJECT" && reload.fieldReference == field && reload.registerA != null &&
            unbox.opcode == "INVOKE_VIRTUAL" && unbox.invokeRegisters == listOf(reload.registerA) &&
            unbox.methodReference == RuleInstructionReference("java.lang.Boolean", "booleanValue", emptyList(), "boolean") &&
            result.opcode == "MOVE_RESULT" && result.registerA != null &&
            returned.opcode == "RETURN" && returned.registerA == result.registerA
    }
}
