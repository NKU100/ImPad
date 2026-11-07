package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.rules.RuleInstruction
import io.github.nku100.impad.rules.RuleInstructionReference
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class WeChatTabletCacheFieldTest {
    @Test
    fun identifiesDirectAndReloadedCacheReturns() {
        assertEquals(FIELD, WeChatTabletCacheField.extract(OWNER, sequence(false)))
        assertEquals(FIELD, WeChatTabletCacheField.extract(OWNER, sequence(true)))
    }

    @Test
    fun unrelatedBooleanWritesDoNotDetermineTheCache() {
        val instructions = sequence(false) + RuleInstruction(24, "SPUT_OBJECT", registerA = 1,
            fieldReference = "Lfixture/Decision;->other:Ljava/lang/Boolean;")
        assertEquals(FIELD, WeChatTabletCacheField.extract(OWNER, instructions))
    }

    @Test
    fun refusesAReadOnlyOrForeignCache() {
        assertNull(WeChatTabletCacheField.extract(OWNER, sequence(false).filter { it.opcode != "SPUT_OBJECT" }))
        assertNull(WeChatTabletCacheField.extract("Lfixture/Other;", sequence(false)))
    }

    @Test
    fun refusesWrongTypeAndInstanceWrites() {
        assertNull(WeChatTabletCacheField.extract(OWNER, sequence(false).map {
            if (it.fieldReference == FIELD) it.copy(fieldReference = "Lfixture/Decision;->cached:Ljava/lang/Object;") else it
        }))
        assertNull(WeChatTabletCacheField.extract(OWNER, sequence(false).map {
            if (it.opcode == "SPUT_OBJECT") it.copy(opcode = "IPUT_OBJECT") else it
        }))
    }

    @Test
    fun refusesReloadOfAnotherFieldAndUncheckedReceiver() {
        assertNull(WeChatTabletCacheField.extract(OWNER, sequence(true).map {
            if (it.codeAddress == 4) it.copy(fieldReference = "Lfixture/Decision;->other:Ljava/lang/Boolean;") else it
        }))
        assertNull(WeChatTabletCacheField.extract(OWNER, sequence(false).map {
            if (it.opcode == "INVOKE_VIRTUAL") it.copy(invokeRegisters = listOf(7)) else it
        }))
    }

    @Test
    fun refusesBranchesIntoTheCachedReturnOrOutsideTheMethod() {
        for (target in listOf(4, 10, 999)) {
            assertNull(WeChatTabletCacheField.extract(OWNER, sequence(false).map {
                if (it.opcode == "IF_EQZ") it.copy(branchTarget = target) else it
            }))
        }
    }

    @Test
    fun refusesUnboundOrDifferentReturnRegisters() {
        assertNull(WeChatTabletCacheField.extract(OWNER, sequence(false).map {
            if (it.opcode == "MOVE_RESULT") it.copy(registerA = null) else it
        }))
        assertNull(WeChatTabletCacheField.extract(OWNER, sequence(false).map {
            if (it.opcode == "RETURN" && it.codeAddress == 10) it.copy(registerA = 7) else it
        }))
    }

    @Test
    fun refusesTwoDistinctCompleteCacheFields() {
        val second = sequence(false).map {
            it.copy(codeAddress = it.codeAddress + 30, branchTarget = it.branchTarget?.plus(30),
                fieldReference = it.fieldReference?.replace("cached", "other"))
        }
        assertNull(WeChatTabletCacheField.extract(OWNER, sequence(false) + second))
    }

    @Test
    fun refusesMalformedInstructionAddressesAndFieldDescriptors() {
        assertNull(WeChatTabletCacheField.extract(OWNER, sequence(false).reversed()))
        assertNull(WeChatTabletCacheField.extract(OWNER, sequence(false).map {
            if (it.fieldReference == FIELD) it.copy(fieldReference = "Lfixture/Decision;->:Ljava/lang/Boolean;") else it
        }))
    }

    @Test
    fun writesOutsideTheColdPathCannotEstablishTheCacheRelationship() {
        val instructions = sequence(false)
        assertNull(WeChatTabletCacheField.extract(OWNER, instructions.map {
            if (it.codeAddress == 12) it.copy(opcode = "RETURN", fieldReference = null) else it
        }))
        assertNull(WeChatTabletCacheField.extract(OWNER,
            listOf(RuleInstruction(0, "SPUT_OBJECT", registerA = 1, fieldReference = FIELD)) +
                instructions.filter { it.opcode != "SPUT_OBJECT" }.map {
                    it.copy(codeAddress = it.codeAddress + 2, branchTarget = it.branchTarget?.plus(2))
                }))
    }

    @Test
    fun malformedOrUnsupportedColdPathControlFlowIsRejected() {
        assertNull(WeChatTabletCacheField.extract(OWNER, sequence(false).map {
            if (it.codeAddress == 12) it.copy(opcode = "GOTO", branchTarget = 999) else it
        }))
        assertNull(WeChatTabletCacheField.extract(OWNER, sequence(false).map {
            if (it.codeAddress == 12) it.copy(opcode = "PACKED_SWITCH") else it
        }))
    }

    private fun sequence(reloaded: Boolean): List<RuleInstruction> {
        val receiver = if (reloaded) 6 else 0
        return listOf(
            RuleInstruction(0, "SGET_OBJECT", registerA = 0, fieldReference = FIELD),
            RuleInstruction(2, "IF_EQZ", registerA = 0, branchTarget = 12),
        ) + (if (reloaded) listOf(RuleInstruction(4, "SGET_OBJECT", registerA = 6, fieldReference = FIELD)) else emptyList()) + listOf(
            RuleInstruction(if (reloaded) 6 else 4, "INVOKE_VIRTUAL", invokeRegisters = listOf(receiver),
                methodReference = RuleInstructionReference("java.lang.Boolean", "booleanValue", emptyList(), "boolean")),
            RuleInstruction(9, "MOVE_RESULT", registerA = 6),
            RuleInstruction(10, "RETURN", registerA = 6),
            RuleInstruction(12, "SGET_OBJECT", registerA = 1, fieldReference = "Ljava/lang/Boolean;->TRUE:Ljava/lang/Boolean;"),
            RuleInstruction(14, "SPUT_OBJECT", registerA = 1, fieldReference = FIELD),
            RuleInstruction(16, "CONST_4", registerA = 1, literal = 1),
            RuleInstruction(17, "RETURN", registerA = 1),
        )
    }

    private companion object {
        const val OWNER = "Lfixture/Decision;"
        const val FIELD = "Lfixture/Decision;->cached:Ljava/lang/Boolean;"
    }
}
