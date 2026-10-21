package io.github.nku100.impad.compat.apps.qq

import io.github.nku100.impad.compat.AppCompatibilityPolicy

object QqCompatibilityPolicy {
    val policy = AppCompatibilityPolicy(
        appId = "qq",
        packageName = "com.tencent.mobileqq",
        supportedAbis = setOf("arm64-v8a"),
        signerSha256 = "ea6e97ad6c34f7039a9c6daba732c97d0e098e83ede2b4d52c76eb0184ac7a38",
        requiredHookIds = setOf("device_type"),
        officialApkPrefix = "https://downv6.qq.com/qqweb/QQ_1/android_apk/",
    )
}
