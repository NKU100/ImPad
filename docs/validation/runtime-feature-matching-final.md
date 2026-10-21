# 三应用共享规则运行验证

微信、QQ 和企业微信的指定基准已分别通过 GitHub-hosted ARM64 ReDroid 运行验收。三次独立验证使用同一份模块 APK 和同一份127项源码身份。

## 被测模块

- 构建 run：[39554279828](https://github.com/NKU100/ImPad/actions/runs/39554279828)，artifact `impad-module-apk`。
- 源码提交：`8690ed5162ba71c1d756f9e57793c774f2501de7`。
- 模块 APK SHA-256：`d8cdaf1aba2d19503c9a9a2679195456657faae1b5fae8e9d52e2c5ba1b88691`。
- 127 项模块源码树 SHA-256：`654e389fd9b35a1362d6d9c79d2f07f8cd249f141fdfff59c74cff1b70f25aae`。

每个 runtime run 的候选模块 SHA 与上述构建 APK 相同。

## 运行结果

| 应用 | 被测官方 APK | GitHub 运行 | 结果 |
| --- | --- | --- | --- |
| QQ 9.3.1 / 14378 | `ccf9714d268662ee8311add537705e66dee3367d45a20eabbf57b0070949595e` | [39555436990](https://github.com/NKU100/ImPad/actions/runs/39555436990) | 应用验收与运行冒烟通过；`qq_device_type_001` 唯一匹配；手机/平板 AppID 为 `537360567` / `537360606`；MSF owner 验证通过，capture 无 kernel drops 和 partial streams；tooling 4/4；production verifier `VERIFIED`。 |
| 微信 8.0.69 / 3040 | `e4da5809f2b3305e375db38897a4082085adc6b51182fa807813c4dda1c3a948` | [39556863774](https://github.com/NKU100/ImPad/actions/runs/39556863774) | 应用验收与运行冒烟通过；`wechat_tablet_login_001` 唯一匹配；稳定显示 `LoginAsExDeviceUI`；Tinker READY，native 由本模块 APK 直接加载，兼容性/native 就绪后安装2个 hook；tooling 4/4；production verifier `VERIFIED`。 |
| 企业微信 5.0.8 / 70675 | `59b3e620a3cf1d1f8740ea320e05fd821bb811040b36f69aa307e7fc8a2ca26d` | [39557803980](https://github.com/NKU100/ImPad/actions/runs/39557803980) | 应用验收与运行冒烟通过；`wecom_local_001` 唯一匹配；manager off/on 状态与 scope 已验证，稳定显示 `LoginQrCodeForAndroidPad`；Tinker READY，native 由本模块 APK 直接加载，兼容性/native 就绪后安装3个 hook；tooling 4/4；production verifier `VERIFIED`。 |

三项固定静态回归均为 `PASSED`，`initialMigration=false`。三个 run 使用 `operation=verify`；未调用 Codex 适配步骤，也未将跳过的适配或登记步骤当作验收证据。

## 验收范围与局限

运行流程未使用真实账号、未扫码、未提交登录。结果证明上述精确应用 APK 与模块 APK 在该 ARM64 ReDroid 流程中的界面、hook、native 与生产证据校验行为；不证明真实账号登录、服务器接受或双设备会话共存，也不代表所有设备、Android 版本或未测试应用版本通过。

Tinker READY 和 native 加载仅是本次精确构建的观测结果，不证明完整热补丁还原或未来任意热补丁变化均受支持。QQ collector 沿用 SYN owner 缓存合同，并在同 tuple、同 ISN 的 SYN 上重新核验 inode 与 UID；未启用逐包全 FD 唯一扫描。SYN 后的任意 socket FD handoff 未被该合同验证，仍是明确局限。

Oracle ARM64 ReDroid 上的四轮尝试未通过，包括另一份历史成功 binary/APK 的精确复测。Oracle framework 文件与权限一致性未获证，环境或部署差异仍待定位；不能据此宣称纯宿主根因。GitHub-hosted 的三次成功结果绑定本节列出的模块身份，不用于解释 Oracle 失败。

QQ owner-contract restoration 的普通 Python 测试为386项、385通过、1项跳过；157项 JVM 测试因 JVM 源码未变化而复用既有结果，没有在该轮重跑。GitHub 构建 run 39554279828 随后独立执行386项 Python 测试并全部通过，且其 Gradle 测试、模块元数据、三应用静态回归和 APK 构建均成功。
