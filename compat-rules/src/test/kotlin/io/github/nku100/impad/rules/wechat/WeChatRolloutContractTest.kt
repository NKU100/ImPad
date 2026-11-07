package io.github.nku100.impad.rules.wechat

import io.github.nku100.impad.compat.rules.*
import io.github.nku100.impad.rules.HookRoleContract
import kotlin.test.*

class WeChatRolloutContractTest {
    private fun hook(config: Map<String,String>) = PlanHook("inner", "Lfixture/Host;->decide(Ljava/lang/String;)Z",
        MethodShape(listOf("java.lang.String"),"boolean",true),TargetCodeSource("root",0,"a".repeat(64)),HookPhase.AFTER,config)
    private val config = mapOf("policy" to "cache_true_after","cacheField" to "Lfixture/Host;->cache:Ljava/lang/Boolean;","monitorField" to "Lfixture/Host;->lock:Ljava/lang/Object;")
    private val role=HookRoleContract("inner",HookPhase.AFTER,mapOf("policy" to "cache_true_after"))
    @Test fun rejectsForeignAndMalformedFieldBindings() {
        validateHookConfiguration(hook(config),role)
        validateHookConfiguration(hook(config+ ("monitorField" to "Lfixture/Host;")),role)
        for(c in listOf(config-("cacheField"),config+("cacheField" to "Lother/Host;->cache:Ljava/lang/Boolean;"),
            config+("monitorField" to "Lfixture/Host;->lock:I"),config+("extra" to "x")))
            assertFailsWith<IllegalArgumentException>{validateHookConfiguration(hook(c),role)}
    }
    @Test fun cacheAndMonitorChangesBothInvalidateExecutionIdentity() {
        fun plan(c:Map<String,String>)=CompletePlan("b".repeat(64),"wechat_tablet_login",2,"wechat_tablet_login_callbacks",2,listOf(hook(c)))
        assertNotEquals(plan(config).executionSha256(),plan(config+("cacheField" to "Lfixture/Host;->newCache:Ljava/lang/Boolean;")).executionSha256())
        assertNotEquals(plan(config).executionSha256(),plan(config+("monitorField" to "Lfixture/Host;")).executionSha256())
    }
}
