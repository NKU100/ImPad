# 已验证版本与验证范围

以下三个精确应用构建已通过同一份 ImPad 模块 APK 的 GitHub-hosted ARM64 ReDroid 运行验收。模块 APK SHA-256 为 `d8cdaf1aba2d19503c9a9a2679195456657faae1b5fae8e9d52e2c5ba1b88691`，源码提交为 `8690ed5162ba71c1d756f9e57793c774f2501de7`，127 项模块源码树 SHA-256 为 `654e389fd9b35a1362d6d9c79d2f07f8cd249f141fdfff59c74cff1b70f25aae`。模块由[构建 run 39554279828](https://github.com/NKU100/ImPad/actions/runs/39554279828)生成；该构建 artifact 为 `impad-module-apk`。

| 应用构建 | 官方 APK SHA-256 | 独立运行验收 | 已验证行为 |
| --- | --- | --- | --- |
| QQ 9.3.1 / 14378 | `ccf9714d268662ee8311add537705e66dee3367d45a20eabbf57b0070949595e` | [39555436990](https://github.com/NKU100/ImPad/actions/runs/39555436990) | `qq_device_type_001` 唯一匹配；手机 AppID `537360567`、平板 AppID `537360606`；MSF owner 已验证，capture 无 kernel drops 或 partial streams；production verifier `VERIFIED`。 |
| 微信 8.0.69 / 3040 | `e4da5809f2b3305e375db38897a4082085adc6b51182fa807813c4dda1c3a948` | [39556863774](https://github.com/NKU100/ImPad/actions/runs/39556863774) | `wechat_tablet_login_001` 唯一匹配；稳定显示 `LoginAsExDeviceUI`；Tinker READY，native 从本模块 APK 加载，2 个 hook 在兼容性和 native 就绪后安装；production verifier `VERIFIED`。 |
| 企业微信 5.0.8 / 70675 | `59b3e620a3cf1d1f8740ea320e05fd821bb811040b36f69aa307e7fc8a2ca26d` | [39557803980](https://github.com/NKU100/ImPad/actions/runs/39557803980) | `wecom_local_001` 唯一匹配；manager 关闭/启用状态及 scope 已验证；稳定显示 `LoginQrCodeForAndroidPad`；Tinker READY，native 从本模块 APK 加载，3 个 hook 在兼容性和 native 就绪后安装；production verifier `VERIFIED`。 |

每次运行还通过 `APPLICATION_ACCEPTANCE_VERIFIED`、`RUNTIME_SMOKE_VERIFIED` 和 tooling 4/4。三项静态固定回归均为 `PASSED`，`initialMigration=false`。各候选的官方 APK URL 分别为 [QQ](https://downv6.qq.com/qqweb/QQ_1/android_apk/9.3.1_877e277185fb829c.apk)、[微信](https://dldir1v6.qq.com/weixin/android/weixin8069android3040_0x2800455a_arm64.apk) 和[企业微信](https://dldir1.qq.com/wework/work_weixin/WeCom_android_5.0.8.70675_arm64_100001.apk)。

验收未使用真实账号、未扫描二维码、未提交登录。结果不证明真实账号登录、服务器接受或双设备会话共存，也不覆盖所有设备、Android 版本或所有热补丁行为。Tinker readiness 和 native 加载只证明上述精确构建在被测流程中的观测结果，不等于对后续任意热补丁变更的兼容保证。

QQ collector 继续使用 SYN 阶段建立的 owner/generation 缓存，并在相同 tuple、相同 ISN 的 SYN 上核对当前 socket inode 与 UID。只有能证明仍是同一 socket 时才沿用缓存；身份变化或无法确认时会重新解析，未知 owner 继续 fail-closed。没有增加逐包全 FD 扫描，也没有验证 SYN 之后的任意 FD handoff；FD handoff 是该历史采集合同保留的明确局限，不应视为已排除风险。

统一模块在 Oracle ARM64 ReDroid 上的四轮尝试未通过，包括复测一份曾在其他运行中成功的精确 binary/APK。Oracle framework 文件与权限一致性未获证，环境或部署差异仍待定位；这些失败不能证明纯宿主原因，也不能据此推断 GitHub-hosted 运行环境有相同问题。上述三个 GitHub-hosted run 是绑定当前模块身份的新运行验收。

其他应用版本或模块构建不由这些结果推定通过。旧 `targets.json` 和 profile 文件已从当前仓库树移除；需要追溯时可从 Git 历史恢复。它们不参与当前模块打包、规则检查、运行准入或流水线。
