package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.rules.RuleInstruction
import io.github.nku100.impad.rules.RuleInstructionReference
import kotlin.test.*

class WeChatCachedBooleanReturnTest {
    @Test
    fun recognizesCacheReloadIntoADifferentRegister() {
        assertTrue(WeChatCachedBooleanReturn.matches(sequence()))
    }

    @Test
    fun differentFieldOrUncheckedReceiverCannotBeUsedAsTheCachedResult() {
        assertFalse(WeChatCachedBooleanReturn.matches(sequence("Lfixture/Other;->cache:Ljava/lang/Boolean;")))
        assertFalse(WeChatCachedBooleanReturn.matches(sequence().map {
            if (it.opcode == "INVOKE_VIRTUAL") it.copy(invokeRegisters = listOf(0)) else it
        }))
    }

    @Test
    fun nonNullBranchMustSkipTheWholeEarlyReturn() {
        assertFalse(WeChatCachedBooleanReturn.matches(sequence().map {
            if (it.opcode == "IF_EQZ") it.copy(branchTarget = 6) else it
        }))
    }

    @Test
    fun originalCacheShapeDoesNotMatchThePatchVariant() {
        assertFalse(WeChatCachedBooleanReturn.matches(sequence().filter { it.codeAddress != 4 }))
    }

    private fun sequence(reloadedField: String = FIELD) = listOf(
        RuleInstruction(0, "SGET_OBJECT", registerA = 0, fieldReference = FIELD),
        RuleInstruction(2, "IF_EQZ", registerA = 0, branchTarget = 12),
        RuleInstruction(4, "SGET_OBJECT", registerA = 6, fieldReference = reloadedField),
        RuleInstruction(6, "INVOKE_VIRTUAL", invokeRegisters = listOf(6),
            methodReference = RuleInstructionReference("java.lang.Boolean", "booleanValue", emptyList(), "boolean")),
        RuleInstruction(9, "MOVE_RESULT", registerA = 6),
        RuleInstruction(10, "RETURN", registerA = 6),
        RuleInstruction(12, "CONST_4", registerA = 0, literal = 0),
    )

    private companion object {
        const val FIELD = "Lfixture/Tablet;->cache:Ljava/lang/Boolean;"
    }
}
