package io.github.nku100.impad.runtime

import org.junit.jupiter.api.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNull
import kotlin.test.assertSame
import kotlin.test.assertTrue

class AppAdapterRegistryTest {
    @Test
    fun adapterContractHasNoProfileOrDescriptorRuntimeSurface() {
        val forbidden = setOf("targetsAssetPath", "resolutionMode", "install")
        assertTrue(AppAdapter::class.java.methods.none { it.name in forbidden })
    }

    @Test
    fun defaultRegistryRoutesAllSupportedAppsInEveryBuildType() {
        val registry = AppAdapterRegistry()
        assertEquals("wechat", registry.find("com.tencent.mm", "com.tencent.mm")?.compatibilityPolicy?.appId)
        assertEquals("qq", registry.find("com.tencent.mobileqq", "com.tencent.mobileqq:MSF")?.compatibilityPolicy?.appId)
        assertEquals("wecom", registry.find("com.tencent.wework", "com.tencent.wework:push")?.compatibilityPolicy?.appId)
        assertNull(registry.find("com.tencent.mobileqq", "com.tencent.mobileqq"))
        assertNull(registry.find("com.tencent.mm", "com.tencent.mm:push"))
    }

    @Test
    fun routesOnlyTheRegisteredPackageAndAcceptedProcess() {
        val adapter = FakeAdapter("example.app")
        val registry = AppAdapterRegistry(listOf(adapter))
        assertSame(adapter, registry.find("example.app", "example.app"))
        assertNull(registry.find("example.app", "example.app:push"))
        assertNull(registry.find("example.app", null))
        assertNull(registry.find("another.app", "another.app"))
    }

    @Test
    fun rejectsMultipleAdaptersForTheSamePackage() {
        assertFailsWith<IllegalArgumentException> {
            AppAdapterRegistry(listOf(FakeAdapter("example.app"), FakeAdapter("example.app")))
        }
    }

    @Test
    fun weChatAdapterKeepsMainProcessRestrictionAndUsesItsOwnRules() {
        val adapter = io.github.nku100.impad.apps.wechat.WeChatAppAdapter()
        val registry = AppAdapterRegistry(listOf(adapter))
        assertSame(adapter, registry.find("com.tencent.mm", "com.tencent.mm"))
        assertNull(registry.find("com.tencent.mm", "com.tencent.mm:push"))
        assertNull(registry.find("com.tencent.mobileqq", "com.tencent.mobileqq:MSF"))
    }

    private class FakeAdapter(override val packageName: String) : AppAdapter {
        override val compatibilityPolicy = io.github.nku100.impad.compat.apps.wechat.WeChatCompatibilityPolicy.policy
        override fun acceptsProcess(processName: String?): Boolean = processName == packageName
    }
}
