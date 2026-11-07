package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.rules.RuleInstruction

/** Identifies the monitor used by the recognized wrapper around its cold decision. */
object WeChatTabletMonitor {
    fun extract(decisionDescriptor: String, instructions: List<RuleInstruction>): String? {
        if (instructions.isEmpty() || instructions.first().codeAddress < 0 ||
            instructions.zipWithNext().any { (a, b) -> a.codeAddress >= b.codeAddress }) return null
        val owner = decisionDescriptor.substringBefore("->")
        val candidates = mutableSetOf<String>()
        for (index in instructions.indices) {
            val read = instructions[index]
            val descriptor = read.fieldReference ?: continue
            if (read.opcode != "SGET_OBJECT" || !descriptor.startsWith("$owner->") ||
                !descriptor.endsWith(":Ljava/lang/Object;") || read.registerA == null) continue
            val enter = instructions.getOrNull(index + 1) ?: continue
            val invoke = instructions.getOrNull(index + 2) ?: continue
            val ref = invoke.methodReference ?: continue
            val signature = "L${ref.owner.replace('.', '/')};->${ref.name}(" +
                ref.parameterTypes.joinToString("") { "L${it.replace('.', '/')};" } + ")Z"
            if (enter.opcode != "MONITOR_ENTER" || enter.registerA != read.registerA ||
                invoke.opcode != "INVOKE_STATIC" || ref.returnType != "boolean" || signature != decisionDescriptor) continue
            val result = instructions.getOrNull(index + 3) ?: continue
            val exit = instructions.getOrNull(index + 4) ?: continue
            if (ref.parameterTypes.size == 1 && invoke.invokeRegisters.size == 1 && result.registerA != null && result.registerA != read.registerA && result.opcode == "MOVE_RESULT" && exit.opcode == "MONITOR_EXIT" && exit.registerA == read.registerA)
                candidates += descriptor
        }
        if (candidates.isNotEmpty()) return candidates.singleOrNull()
        // A proved direct forwarding wrapper has no shared monitor; synchronize corrections on its declaring class.
        val calls = instructions.withIndex().filter { (_, i) ->
            i.opcode == "INVOKE_STATIC" && i.methodReference?.let { ref ->
                "L${ref.owner.replace('.', '/')};->${ref.name}(" + ref.parameterTypes.joinToString("") { "L${it.replace('.', '/')};" } + ")Z"
            } == decisionDescriptor
        }
        val call = calls.singleOrNull() ?: return null
        if (instructions.any { it.opcode.startsWith("IF_") || it.opcode.startsWith("GOTO") || it.opcode.contains("SWITCH") ||
            it.opcode.startsWith("MONITOR") }) return null
        val result = instructions.getOrNull(call.index + 1) ?: return null
        val returned = instructions.getOrNull(call.index + 2) ?: return null
        return owner.takeIf { call.value.invokeRegisters.size == 1 && result.opcode == "MOVE_RESULT" && result.registerA != null &&
            returned.opcode == "RETURN" && returned.registerA == result.registerA && call.index + 2 == instructions.lastIndex }

    }
}
