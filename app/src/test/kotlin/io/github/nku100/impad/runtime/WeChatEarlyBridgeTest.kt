package io.github.nku100.impad.runtime
import kotlin.test.*

class WeChatEarlyBridgeTest {
    @Test fun invokesEarlyInstallationBeforeOriginalAndNestedCallDoesNotRetire() {
        val events=mutableListOf<String>();lateinit var cb:HookCallback
        val registrar=HookRegistrar { _,_,callback -> cb=callback;HookRegistration{} }
        val bridge=WeChatEarlyClassLoaderBridge(registrar)
        bridge.installObserved(Host::class.java.getDeclaredMethod("fence"),{ Host::class.java.classLoader!! },{ _, _ ->
            events += "install"
            cb.intercept(HookCall(Host(),emptyArray()){events += "nestedOriginal"})
        },{events += "retire"},{throw it})
        cb.intercept(HookCall(Host(),emptyArray()){events += "original";cb.intercept(HookCall(Host(),emptyArray()){ events += "nestedDuringOriginal" })})
        assertEquals(listOf("install","nestedOriginal","original","nestedDuringOriginal"),events)
        cb.intercept(HookCall(Host(),emptyArray()){events += "laterOriginal"})
        assertEquals(listOf("install","nestedOriginal","original","nestedDuringOriginal","retire","laterOriginal"),events)
    }
    @Test fun preparationFailureStillCallsOriginalExactlyOnce() {
        var original=0;var errors=0;lateinit var cb:HookCallback
        val registrar=HookRegistrar{_,_,callback->cb=callback;HookRegistration{}}
        WeChatEarlyClassLoaderBridge(registrar).installObserved(Host::class.java.getDeclaredMethod("fence"),
            {Host::class.java.classLoader!!},{ _, _ -> error("prepare")},{},{errors++;error("report failed")})
        cb.intercept(HookCall(Host(),emptyArray()){original++})
        assertEquals(1,original);assertEquals(1,errors)
    }
    class Host { fun fence(){} }
}
