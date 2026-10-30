package io.github.nku100.impad.runtime

import android.content.Context
import java.lang.reflect.Method
import java.util.concurrent.CountDownLatch
import java.util.concurrent.atomic.AtomicBoolean

class PostAttachClassLoaderBridge(private val registrar: HookRegistrar) {
    private enum class MonitoredState { WAITING, INITIALIZING, MONITORING, RETIRING, RETIRED }

    fun install(
        method: Method,
        hookId: String,
        classLoaderAfterAttach: (HookCall) -> ClassLoader,
        onReady: (ClassLoader) -> Unit,
    ): HookRegistration = installReady(method, hookId, { call ->
        val loader = classLoaderAfterAttach(call)
        null to loader
    }) { _, loader -> onReady(loader) }

    fun installWithContext(
        method: Method,
        hookId: String,
        onReady: (Context, ClassLoader) -> Unit,
    ): HookRegistration = installReady(method, hookId, { call ->
        val context = checkNotNull(call.arguments.firstOrNull() as? Context) {
            "QFix attach did not provide its Context"
        }
        context to checkNotNull(context.classLoader) { "QFix Context did not provide a ClassLoader" }
    }) { context, loader -> onReady(checkNotNull(context), loader) }

    fun installMonitoredWithContext(
        method: Method,
        hookId: String,
        onReady: (Context, ClassLoader) -> Unit,
        onLaterReady: () -> Unit,
    ): HookRegistration = installMonitoredWithContext(
        method, hookId,
        classLoaderAfterAttach = { call ->
            val context = checkNotNull(call.arguments.firstOrNull() as? Context) {
                "Class-loader attach did not provide its Context"
            }
            checkNotNull(context.classLoader) { "Attach Context did not provide a ClassLoader" }
        },
        onReady = onReady,
        onLaterReady = onLaterReady,
    )

    fun installMonitoredWithContext(
        method: Method,
        hookId: String,
        classLoaderAfterAttach: (HookCall) -> ClassLoader,
        onReady: (Context, ClassLoader) -> Unit,
        onLaterReady: () -> Unit,
    ): HookRegistration {
        val stateLock = Any()
        val registrationLock = Any()
        val retirementComplete = CountDownLatch(1)
        var state = MonitoredState.WAITING
        var registration: HookRegistration? = null
        var deferredUnhook = false
        var deferredFailure: Throwable? = null
        fun unhook(primaryError: Throwable? = null) {
            val installed = synchronized(registrationLock) {
                registration ?: run {
                    deferredUnhook = true
                    if (primaryError != null) deferredFailure = primaryError
                    null
                }
            }
            if (installed != null) {
                try {
                    installed.unhook()
                } catch (cleanupError: Throwable) {
                    if (primaryError == null) throw cleanupError
                    if (cleanupError !== primaryError) primaryError.addSuppressed(cleanupError)
                }
            }
        }
        val installed = registrar.hook(method, hookId) { call ->
            val entry = synchronized(stateLock) {
                when (state) {
                    MonitoredState.WAITING -> { state = MonitoredState.INITIALIZING; MonitoredState.INITIALIZING }
                    MonitoredState.INITIALIZING -> MonitoredState.WAITING
                    MonitoredState.MONITORING -> { state = MonitoredState.RETIRING; MonitoredState.MONITORING }
                    MonitoredState.RETIRING -> MonitoredState.RETIRING
                    MonitoredState.RETIRED -> MonitoredState.RETIRED
                }
            }
            when (entry) {
                MonitoredState.INITIALIZING -> {
                    try {
                        val result = call.proceed()
                        val context = checkNotNull(call.arguments.firstOrNull() as? Context) {
                            "Class-loader attach did not provide its Context"
                        }
                        val loader = classLoaderAfterAttach(call)
                        onReady(context, loader)
                        synchronized(stateLock) { if (state == MonitoredState.INITIALIZING) state = MonitoredState.MONITORING }
                        result
                    } catch (error: Throwable) {
                        synchronized(stateLock) { state = MonitoredState.RETIRED }
                        unhook(error)
                        throw error
                    }
                }
                MonitoredState.MONITORING -> {
                    try {
                        try {
                            onLaterReady()
                        } catch (error: Throwable) {
                            if (error is VirtualMachineError || error is ThreadDeath) throw error
                        }
                        try {
                            unhook()
                        } catch (error: Throwable) {
                            if (error is VirtualMachineError || error is ThreadDeath) throw error
                        }
                    } catch (error: Throwable) {
                        if (error is VirtualMachineError || error is ThreadDeath) throw error
                    } finally {
                        synchronized(stateLock) {
                            state = MonitoredState.RETIRED
                            retirementComplete.countDown()
                        }
                    }
                    call.proceed()
                }
                MonitoredState.RETIRING -> {
                    var interrupted = false
                    while (true) {
                        try {
                            retirementComplete.await()
                            break
                        } catch (_: InterruptedException) {
                            interrupted = true
                        }
                    }
                    if (interrupted) Thread.currentThread().interrupt()
                    call.proceed()
                }
                MonitoredState.WAITING, MonitoredState.RETIRED -> call.proceed()
            }
        }
        val deferred = synchronized(registrationLock) {
            registration = installed
            deferredUnhook to deferredFailure
        }
        if (deferred.first) {
            try {
                installed.unhook()
            } catch (cleanupError: Throwable) {
                val primaryError = deferred.second
                if (primaryError == null) {
                    if (cleanupError is VirtualMachineError || cleanupError is ThreadDeath) throw cleanupError
                } else if (cleanupError !== primaryError) {
                    primaryError.addSuppressed(cleanupError)
                }
            }
        }
        deferred.second?.let { throw it }
        return installed
    }

    fun installWithContext(
        method: Method,
        hookId: String,
        classLoaderAfterAttach: (HookCall) -> ClassLoader,
        onReady: (Context, ClassLoader) -> Unit,
    ): HookRegistration = installReady(method, hookId, { call ->
        val context = checkNotNull(call.arguments.firstOrNull() as? Context) {
            "Class-loader attach did not provide its Context"
        }
        context to classLoaderAfterAttach(call)
    }) { context, loader -> onReady(checkNotNull(context), loader) }

    private fun installReady(
        method: Method,
        hookId: String,
        readyInfo: (HookCall) -> Pair<Context?, ClassLoader>,
        onReady: (Context?, ClassLoader) -> Unit,
    ): HookRegistration {
        // Claim on entry so a nested attach cannot install before its outer call returns.
        val claimed = AtomicBoolean(false)
        var registration: HookRegistration? = null
        registration = registrar.hook(method, hookId) { call ->
            val first = claimed.compareAndSet(false, true)
            try {
                val result = call.proceed()
                if (first) {
                    val (context, loader) = readyInfo(call)
                    onReady(context, loader)
                }
                result
            } finally {
                if (first) registration?.unhook()
            }
        }
        return registration
    }
}
