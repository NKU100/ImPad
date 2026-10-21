package io.github.nku100.impad.compat.apps.wecom

import io.github.nku100.impad.compat.AppCompatibilityPolicy

object WeComCompatibilityPolicy {
    private const val SIGNER_SHA256 = "66896ae0aae48b9e963a3e034fa2ca813ca2f661f53619a92263a8a5c2e3f388"

    val policy = AppCompatibilityPolicy(
        appId = "wecom",
        packageName = "com.tencent.wework",
        supportedAbis = setOf("arm64-v8a"),
        signerSha256 = SIGNER_SHA256,
        requiredHookIds = setOf("device_type", "app_pad", "service_pad"),
        officialApkPrefix = "https://dldir1.qq.com/wework/work_weixin/",
    )

}
