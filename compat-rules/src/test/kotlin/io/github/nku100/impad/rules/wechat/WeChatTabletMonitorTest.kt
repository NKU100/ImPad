package io.github.nku100.impad.rules.wechat
import io.github.nku100.impad.rules.RuleInstruction
import kotlin.test.*

class WeChatTabletMonitorTest {
    private val decision = "Lfixture/Host;->decide(Ljava/lang/String;)Z"
    private val field = "Lfixture/Host;->lock:Ljava/lang/Object;"
    private fun code() = listOf(
        RuleInstruction(0,"SGET_OBJECT",registerA=2,fieldReference=field),
        RuleInstruction(2,"MONITOR_ENTER",registerA=2),
        RuleInstruction(3,"INVOKE_STATIC",invokeRegisters=listOf(1),methodReference=io.github.nku100.impad.rules.RuleInstructionReference("fixture.Host","decide",listOf("java.lang.String"),"boolean")),
        RuleInstruction(6,"MOVE_RESULT",registerA=1),
        RuleInstruction(7,"MONITOR_EXIT",registerA=2),
        RuleInstruction(8,"RETURN",registerA=1))
    @Test fun extractsActualLockAroundDecision() { assertEquals(field, WeChatTabletMonitor.extract(decision,code())) }
    @Test fun directWrapperUsesClassLockButDoesNotAcceptMissingMonitorExit() {
        val direct = code().filter { it.opcode !in setOf("SGET_OBJECT", "MONITOR_ENTER", "MONITOR_EXIT") }
        assertEquals("Lfixture/Host;", WeChatTabletMonitor.extract(decision, direct))
        assertNull(WeChatTabletMonitor.extract(decision, code().filter { it.opcode == "MONITOR_EXIT" }.let { code().dropLast(2) }))
    }
    @Test fun rejectsWrongRegisterOwnerAndCall() {
        assertNull(WeChatTabletMonitor.extract(decision,code().map { if(it.opcode=="MONITOR_ENTER") it.copy(registerA=3) else it }))
        assertNull(WeChatTabletMonitor.extract(decision,code().map { if(it.opcode=="SGET_OBJECT") it.copy(fieldReference="Lother/Host;->lock:Ljava/lang/Object;") else it }))
        assertNull(WeChatTabletMonitor.extract("Lfixture/Host;->other(Ljava/lang/String;)Z",code()))
        assertNull(WeChatTabletMonitor.extract(decision,code().filterNot { it.opcode=="MONITOR_EXIT" }))
    }
}
