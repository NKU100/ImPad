package io.github.nku100.impad.apps.wecom

import android.util.Log
import io.github.nku100.impad.compat.apps.wecom.WeComCompatibilityPolicy
import io.github.nku100.impad.runtime.AppAdapter
import io.github.nku100.impad.runtime.HookRegistrar
import io.github.nku100.impad.runtime.InstallOutcome
import io.github.nku100.impad.runtime.ModuleLog
import io.github.nku100.impad.runtime.TargetMethodResolver
import io.github.nku100.impad.runtime.TinkerClassLoaderBridge

class WeComAppAdapter : AppAdapter {
    override val compatibilityPolicy = WeComCompatibilityPolicy.policy

    override fun acceptsProcess(processName: String?): Boolean =
        processName == packageName || processName?.startsWith("$packageName:") == true

    override fun install(
        classLoader: ClassLoader,
        descriptors: Map<String, String>,
        registrar: HookRegistrar,
        log: ModuleLog,
    ) {
        TinkerClassLoaderBridge(registrar).install(classLoader, "impad_wecom_tinker_bootstrap") { readyClassLoader ->
            installAfterTinker(readyClassLoader, descriptors, registrar, log)
        }
        log.write(Log.INFO, "waiting for Tinker class loader", null)
    }

    private fun installAfterTinker(
        classLoader: ClassLoader,
        descriptors: Map<String, String>,
        registrar: HookRegistrar,
        log: ModuleLog,
    ) {
        try {
            val deviceTypeMethod = TargetMethodResolver.resolve(
                classLoader,
                requireNotNull(descriptors["device_type"]) { "Compatible result omitted device_type" },
            )
            val applicationPadMethod = TargetMethodResolver.resolve(
                classLoader,
                requireNotNull(descriptors["app_pad"]) { "Compatible result omitted app_pad" },
            )
            val servicePadMethod = TargetMethodResolver.resolve(
                classLoader,
                requireNotNull(descriptors["service_pad"]) { "Compatible result omitted service_pad" },
            )
            when (val outcome = WeComHookInstaller(registrar).install(
                deviceTypeMethod,
                applicationPadMethod,
                servicePadMethod,
            )) {
                is InstallOutcome.Installed -> log.write(
                    Log.INFO,
                    "installed ${outcome.registrations.size} WeCom hooks",
                    null,
                )
                is InstallOutcome.Failed -> log.write(Log.ERROR, "WeCom hook installation failed: ${outcome.reason}", null)
            }
        } catch (error: Throwable) {
            if (error is VirtualMachineError || error is ThreadDeath) throw error
            log.write(Log.ERROR, "WeCom target resolution failed: ${error.message ?: error.javaClass.simpleName}", error)
        }
    }
}
