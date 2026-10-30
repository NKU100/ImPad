package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.rules.RuleInstruction
import io.github.nku100.impad.rules.RuleInstructionReference
import kotlin.test.Test
import kotlin.test.assertFailsWith
import io.github.nku100.impad.rules.AnalysisIncompleteException
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class WeChatLoginControlFlowTest {
    @Test
    fun cachedWrapperMustForwardBothItsCacheAndItsSynchronizedColdResult() {
        val field = "Lfixture/Tablet;->cached:Ljava/lang/Boolean;"
        val code = listOf(
            RuleInstruction(0, "SGET_OBJECT", registerA = 0, fieldReference = field),
            RuleInstruction(2, "IF_EQZ", registerA = 0, branchTarget = 10),
            RuleInstruction(4, "INVOKE_VIRTUAL", invokeRegisters = listOf(0),
                methodReference = RuleInstructionReference("java.lang.Boolean", "booleanValue", emptyList(), "boolean")),
            RuleInstruction(7, "MOVE_RESULT", registerA = 0),
            RuleInstruction(8, "GOTO", branchTarget = 20),
            RuleInstruction(10, "SGET_OBJECT", registerA = 0),
            RuleInstruction(12, "MONITOR_ENTER", registerA = 0),
            RuleInstruction(13, "INVOKE_STATIC", methodReference = tablet),
            RuleInstruction(16, "MOVE_RESULT", registerA = 1),
            RuleInstruction(17, "MONITOR_EXIT", registerA = 0),
            RuleInstruction(18, "MOVE", registerA = 0, registerB = 1),
            RuleInstruction(20, "RETURN", registerA = 0),
        )
        assertTrue(WeChatLoginControlFlow.forwardsCachedBooleanResult(code, tablet, field))
        val shifted = code.map { instruction -> instruction.copy(codeAddress = instruction.codeAddress + 4,
            branchTarget = instruction.branchTarget?.plus(4)) }
        val bypass = listOf(RuleInstruction(0, "IF_EQZ", registerA = 1, branchTarget = 4),
            RuleInstruction(2, "CONST_4", registerA = 0, literal = 0), RuleInstruction(3, "RETURN", registerA = 0)) + shifted
        assertFalse(WeChatLoginControlFlow.forwardsCachedBooleanResult(bypass, tablet, field))
        assertFalse(WeChatLoginControlFlow.forwardsCachedBooleanResult(
            listOf(RuleInstruction(0, "GOTO", branchTarget = 24)) + shifted, tablet, field))
        val forcedCode = listOf(
            RuleInstruction(0, "SGET_BOOLEAN", registerA = 2, fieldReference = "Lfixture/Flags;->force:Z"),
            RuleInstruction(2, "IF_EQZ", registerA = 2, branchTarget = 8),
            RuleInstruction(4, "CONST_4", registerA = 0, literal = 1),
            RuleInstruction(5, "GOTO", branchTarget = 28),
        ) + code.map { it.copy(codeAddress = it.codeAddress + 8, branchTarget = it.branchTarget?.plus(8)) }
        assertTrue(WeChatLoginControlFlow.forwardsCachedBooleanResult(forcedCode, tablet, field))
        for (replacement in listOf(
            forcedCode[2].copy(literal = 0), forcedCode[2].copy(registerA = 1),
        )) assertFalse(WeChatLoginControlFlow.forwardsCachedBooleanResult(
            forcedCode.toMutableList().apply { set(2, replacement) }, tablet, field))
        assertFalse(WeChatLoginControlFlow.forwardsCachedBooleanResult(code, tablet, "other"))
        assertFalse(WeChatLoginControlFlow.forwardsCachedBooleanResult(code.map {
            if (it.codeAddress == 18) it.copy(opcode = "CONST_4", literal = 0) else it
        }, tablet, field))
        assertFalse(WeChatLoginControlFlow.forwardsCachedBooleanResult(code.map {
            if (it.codeAddress == 2) it.copy(branchTarget = 20) else it
        }, tablet, field))
    }

    private val tablet = RuleInstructionReference("fixture.Tablet", "decide", emptyList(), "boolean")
    private val helper = RuleInstructionReference("fixture.Tablet", "isTablet", emptyList(), "boolean")
    private val visibility = RuleInstructionReference("android.view.View", "setVisibility", listOf("int"), "void")

    @Test
    fun visibilityTraversalBudgetExhaustionIsNotReportedAsNoMatch() {
        val code = listOf(
            RuleInstruction(0, "INVOKE_STATIC", methodReference = helper),
            RuleInstruction(1, "MOVE_RESULT", registerA = 0),
            RuleInstruction(2, "IF_NEZ", registerA = 0, branchTarget = 305),
        ) + (3..302).map { RuleInstruction(it, "NOP") } + listOf(
            RuleInstruction(303, "INVOKE_VIRTUAL", methodReference = visibility),
            RuleInstruction(304, "RETURN_VOID"), RuleInstruction(305, "RETURN_VOID"),
        )
        assertFailsWith<AnalysisIncompleteException> { WeChatLoginControlFlow.controlsVisibility(code, helper) }
    }

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
