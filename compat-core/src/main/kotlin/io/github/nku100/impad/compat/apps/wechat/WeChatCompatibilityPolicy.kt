package io.github.nku100.impad.compat.apps.wechat

import io.github.nku100.impad.compat.AppCompatibilityPolicy

object WeChatCompatibilityPolicy {
    val policy = AppCompatibilityPolicy(
        appId = "wechat",
        packageName = "com.tencent.mm",
        supportedAbis = setOf("arm64-v8a"),
        signerSha256 = "0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c",
        requiredHookIds = setOf("tablet", "login"),
        officialApkPrefix = "https://dldir1v6.qq.com/weixin/android/",
    )
}
