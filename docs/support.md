# 支持的应用构建

清单来自仓库中随模块打包的正式 profile。所有条目均为 `arm64-v8a`；debug 和 release 均注册三个应用适配器。profile 验证状态来自历次适配验收，最初使用 debug 模块；下表单独标明使用 ImPad 0.1.0 release APK 完成的运行验收。

模块同时匹配包名、版本名、versionCode、ABI、APK SHA-256 和签名 SHA-256。相同版本名不代表相同 APK。未登记构建不会安装 Hook。

`runtime-verified-local` 表示本地运行验证；`runtime-verified-hosted` 表示托管环境独立运行验收。两者均遵循不输入真实账号、不扫码、不提交登录的验证设计。

## 微信

包名：`com.tencent.mm`

| 版本 | versionCode | APK SHA-256 | profile 验证状态 | 0.1.0 release 运行验收 |
| --- | --- | --- | --- | --- |
| 8.0.69 | 3040 | `e4da5809f2b3305e375db38897a4082085adc6b51182fa807813c4dda1c3a948` | `runtime-verified-local` | 未重跑 |
| 8.0.79 | 3200 | `5feb100337981467fd257c3ad66bb171f54a69d2579b2ecc70d5a628db8e7282` | `runtime-verified-hosted` | 通过 |
| 8.0.79 | 3200 | `f2a725bc04302ad218a563533ae79356e66b64aa711b275bc919a8d432f7e15d` | `runtime-verified-hosted` | 通过 |

签名证书 SHA-256：`0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c`

完整 profile 与官方来源：[compatibility/wechat/targets.json](../compatibility/wechat/targets.json)。

## QQ

包名：`com.tencent.mobileqq`

| 版本 | versionCode | APK SHA-256 | profile 验证状态 | 0.1.0 release 运行验收 |
| --- | --- | --- | --- | --- |
| 9.3.1 | 14378 | `ccf9714d268662ee8311add537705e66dee3367d45a20eabbf57b0070949595e` | `runtime-verified-hosted` | 未重跑 |
| 9.3.70 | 16410 | `e8c28713b740c18246989d47916d9fbbad06d440778a5aab3f4e108a2423e55c` | `runtime-verified-hosted` | 通过 |

签名证书 SHA-256：`ea6e97ad6c34f7039a9c6daba732c97d0e098e83ede2b4d52c76eb0184ac7a38`

完整 profile 与官方来源：[compatibility/qq/targets.json](../compatibility/qq/targets.json)。

## 企业微信

包名：`com.tencent.wework`

| 版本 | versionCode | APK SHA-256 | profile 验证状态 | 0.1.0 release 运行验收 |
| --- | --- | --- | --- | --- |
| 5.0.8 | 70675 | `59b3e620a3cf1d1f8740ea320e05fd821bb811040b36f69aa307e7fc8a2ca26d` | `runtime-verified-local` | 未重跑 |
| 5.0.12 | 83835 | `6b3cead315fa7d8c302b4b538f25abe5afc935b69f9cd49ee212a780f6afdf40` | `runtime-verified-hosted` | 通过 |

签名证书 SHA-256：`66896ae0aae48b9e963a3e034fa2ca813ca2f661f53619a92263a8a5c2e3f388`

完整 profile 与官方来源：[compatibility/wecom/targets.json](../compatibility/wecom/targets.json)。

## ImPad 0.1.0 release 验收证据

2026-10-18 的[构建 CI](https://github.com/NKU100/ImPad/actions/runs/39131683793) 对清单中的全部 7 份 APK 完成静态回归，并生成稳定签名的非 debuggable release APK。源码提交为 `56b1eca95b91aa072da70fa95fa7f228d32e69e2`。

四轮独立运行验收均复用该次构建的同一份模块 APK，未重新构建 debug：

| 应用构建 | APK SHA-256 前缀 | 独立运行验收 |
| --- | --- | --- |
| 微信 8.0.79 / 3200 | `5feb1003` | [39132731980](https://github.com/NKU100/ImPad/actions/runs/39132731980) |
| 微信 8.0.79 / 3200 | `f2a725bc` | [39133311611](https://github.com/NKU100/ImPad/actions/runs/39133311611) |
| QQ 9.3.70 / 16410 | `e8c28713` | [39133831034](https://github.com/NKU100/ImPad/actions/runs/39133831034) |
| 企业微信 5.0.12 / 83835 | `6b3cead3` | [39135858231](https://github.com/NKU100/ImPad/actions/runs/39135858231) |

模块 APK SHA-256：`9e20021049e027ae7a76897b3a4afef2fe24ddc5a3d4a3cb14b707ae0522231a`。

模块签名证书 SHA-256：`d1e2f14a6851822874a6753041ea5681f91ad33e7ec0d9fd264d52f174b8b409`。

微信 8.0.69、QQ 9.3.1、企业微信 5.0.8 保留既有运行验证，并通过本次静态回归；本轮未使用 release 模块重跑这三个旧构建的运行验收。

## 验收边界

微信和企微验证登录入口、二维码页及当前进程 Hook；QQ 验证自然请求的设备身份变化和完整请求证据。清单不承诺真实账号登录、服务端接受或双设备会话共存，也不代表全部 Android 版本和实体设备均通过。
