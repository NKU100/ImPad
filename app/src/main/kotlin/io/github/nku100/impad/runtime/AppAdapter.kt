package io.github.nku100.impad.runtime

import android.content.Context
import io.github.nku100.impad.compat.rules.ResolvedPlan

interface AppAdapter {
    val compatibilityPolicy: io.github.nku100.impad.compat.AppCompatibilityPolicy
    val packageName: String get() = compatibilityPolicy.packageName
    val classLoaderReadyEvent: String get() = "qfix_ready"
    fun acceptsProcess(processName: String?): Boolean
    fun prepareClassLoader(
        loader: ClassLoader,
        registrar: HookRegistrar,
        onReady: (Context, ClassLoader) -> Unit,
    ) {
        error("${compatibilityPolicy.appId} does not provide a rule class-loader bridge")
    }

    fun installPlan(
        loader: ClassLoader,
        plan: ResolvedPlan,
        registrar: HookRegistrar,
        log: ModuleLog,
        inputsStillCurrent: () -> Boolean,
    ): PlanInstallOutcome = PlanInstallOutcome.Failed("${compatibilityPolicy.appId} does not install rule plans")
}

sealed interface PlanInstallOutcome {
    data class Installed(val registrations: List<HookRegistration>) : PlanInstallOutcome
    data object StaleInputs : PlanInstallOutcome
    data class Failed(val reason: String) : PlanInstallOutcome
}

fun interface ModuleLog {
    fun write(priority: Int, message: String, error: Throwable?)
}
