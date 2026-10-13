package io.github.nku100.impad.apps.qq

import android.util.Log
import io.github.nku100.impad.compat.apps.qq.QqCompatibilityPolicy
import io.github.nku100.impad.runtime.*

class QqAppAdapter : AppAdapter {
    override val compatibilityPolicy = QqCompatibilityPolicy.policy

    override fun acceptsProcess(processName: String?): Boolean = processName == "$packageName:MSF"

    override fun install(classLoader: ClassLoader, descriptors: Map<String, String>,
                         registrar: HookRegistrar, log: ModuleLog) {
        try {
            val deviceType = TargetMethodResolver.resolve(classLoader, requireNotNull(descriptors["device_type"]))
            when (val result = QqHookInstaller(registrar).install(deviceType)) {
                is InstallOutcome.Installed -> log.write(Log.INFO, "installed ${result.registrations.size} QQ hooks", null)
                is InstallOutcome.Failed -> log.write(Log.ERROR, "QQ hook installation failed: ${result.reason}", null)
            }
        } catch (error: Throwable) {
            if (error is VirtualMachineError || error is ThreadDeath) throw error
            log.write(Log.ERROR, "QQ target resolution failed: ${error.message ?: error.javaClass.simpleName}", error)
        }
    }
}
