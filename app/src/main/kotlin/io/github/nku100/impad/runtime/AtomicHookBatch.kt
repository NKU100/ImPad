package io.github.nku100.impad.runtime

import java.util.concurrent.atomic.AtomicBoolean

sealed interface AtomicBatchOutcome {
    data class Installed(val batch: AtomicHookBatch) : AtomicBatchOutcome
    data object InputsChanged : AtomicBatchOutcome
    data class Failed(val reason: String, val restartRequired: Boolean) : AtomicBatchOutcome
}

class AtomicHookBatch private constructor(
    private val enabled: AtomicBoolean,
    private val registrations: List<HookRegistration>,
) {
    private var rollbackFailures: List<String>? = null

    @Synchronized
    fun disableAndUnhook(): List<String> {
        enabled.set(false)
        rollbackFailures?.let { return it }
        val failures = mutableListOf<String>()
        var fatal: Throwable? = null
        registrations.asReversed().forEach { registration ->
            try { registration.unhook() } catch (error: Throwable) {
                failures += error.message ?: error.javaClass.simpleName
                if (error is VirtualMachineError || error is ThreadDeath) fatal = fatal ?: error
            }
        }
        rollbackFailures = java.util.Collections.unmodifiableList(failures.toList())
        fatal?.let { throw it }
        return rollbackFailures!!
    }

    companion object {
        fun install(specs: List<HookSpec>, registrar: HookRegistrar, inputsStillCurrent: () -> Boolean): AtomicBatchOutcome {
            val enabled = AtomicBoolean(false)
            val registrations = mutableListOf<HookRegistration>()
            try {
                require(specs.isNotEmpty() && specs.all { it.id.isNotBlank() } && specs.map { it.id }.distinct().size == specs.size) { "Hook IDs must be nonempty and unique" }
                specs.forEach { spec ->
                    registrations += registrar.hook(spec.method, spec.id, HookCallback { call ->
                        if (enabled.get()) spec.callback.intercept(call) else call.proceed()
                    })
                }
                val batch = AtomicHookBatch(enabled, registrations.toList())
                if (!inputsStillCurrent()) {
                    val failures = batch.disableAndUnhook()
                    return if (failures.isEmpty()) AtomicBatchOutcome.InputsChanged
                    else AtomicBatchOutcome.Failed("Inputs changed; rollback failed: ${failures.joinToString()}", restartRequired = true)
                }
                enabled.set(true)
                return AtomicBatchOutcome.Installed(batch)
            } catch (error: Throwable) {
                val failures = AtomicHookBatch(enabled, registrations.toList()).disableAndUnhook()
                if (error is VirtualMachineError || error is ThreadDeath) throw error
                return AtomicBatchOutcome.Failed(buildString {
                    append(error.message ?: error.javaClass.simpleName)
                    if (failures.isNotEmpty()) append("; rollback failed: ").append(failures.joinToString())
                }, restartRequired = failures.isNotEmpty())
            }
        }
    }
}
