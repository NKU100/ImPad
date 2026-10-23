package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.rules.RuleInstruction
import io.github.nku100.impad.rules.RuleInstructionReference
import kotlin.test.Test
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class WeChatLoginControlFlowTest {
    private val tablet = RuleInstructionReference("fixture.Tablet", "decide", emptyList(), "boolean")
    private val helper = RuleInstructionReference("fixture.Tablet", "isTablet", emptyList(), "boolean")
    private val visibility = RuleInstructionReference("android.view.View", "setVisibility", listOf("int"), "void")

    @Test
    fun wrapperMustReturnTheInvokedTabletResult() {
        val code = listOf(
            RuleInstruction(0, "INVOKE_STATIC", methodReference = tablet),
            RuleInstruction(1, "MOVE_RESULT", registerA = 0),
            RuleInstruction(2, "RETURN", registerA = 0),
        )
        assertTrue(WeChatLoginControlFlow.forwardsBooleanResult(code, tablet))
        assertFalse(WeChatLoginControlFlow.forwardsBooleanResult(
            code.take(2) + RuleInstruction(2, "CONST_4", registerA = 0, literal = 1) +
                RuleInstruction(3, "RETURN", registerA = 0), tablet,
        ))
        assertFalse(WeChatLoginControlFlow.forwardsBooleanResult(
            listOf(RuleInstruction(0, "IF_EQZ", registerA = 1, branchTarget = 3)) + code,
            tablet,
        ))
    }

    @Test
    fun loginBooleanMustControlWhetherVisibilityIsSet() {
        val code = listOf(
            RuleInstruction(0, "INVOKE_STATIC", methodReference = helper),
            RuleInstruction(1, "MOVE_RESULT", registerA = 0),
            RuleInstruction(2, "IF_NEZ", registerA = 0, branchTarget = 5),
            RuleInstruction(3, "INVOKE_VIRTUAL", methodReference = visibility),
            RuleInstruction(4, "RETURN_VOID"),
            RuleInstruction(5, "RETURN_VOID"),
        )
        assertTrue(WeChatLoginControlFlow.controlsVisibility(code, helper))
        assertFalse(WeChatLoginControlFlow.controlsVisibility(
            code.filter { it.codeAddress !in setOf(1, 2) }, helper,
        ))
        assertFalse(WeChatLoginControlFlow.controlsVisibility(
            code.map { if (it.codeAddress == 2) it.copy(registerA = 1) else it }, helper,
        ))
        assertFalse(WeChatLoginControlFlow.controlsVisibility(
            code.dropLast(1) + RuleInstruction(5, "GOTO", branchTarget = 3), helper,
        ))
    }
}
