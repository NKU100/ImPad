package io.github.nku100.impad.runtime

class AppAdapterRegistry(
    adapters: List<AppAdapter> = listOf(
        io.github.nku100.impad.apps.wechat.WeChatAppAdapter(),
        io.github.nku100.impad.apps.qq.QqAppAdapter(),
        io.github.nku100.impad.apps.wecom.WeComAppAdapter(),
    ),
) {
    private val byPackage = adapters.associateBy(AppAdapter::packageName)

    init {
        require(byPackage.size == adapters.size) { "Each package must have exactly one app adapter" }
    }

    fun forPackage(packageName: String): AppAdapter? = byPackage[packageName]

    fun find(packageName: String, processName: String?): AppAdapter? =
        forPackage(packageName)?.takeIf { it.acceptsProcess(processName) }
}
