# 已验证版本与验证范围

以下是历史验证范围，不代表当前工作树或其他模块构建已通过相同的运行验收。QQ 9.3.1（14378）和企业微信 5.0.8（70675）的指定官方 APK SHA-256 分别为 `ccf9714d268662ee8311add537705e66dee3367d45a20eabbf57b0070949595e` 和 `59b3e620a3cf1d1f8740ea320e05fd821bb811040b36f69aa307e7fc8a2ca26d`。

这两项历史 gate 绑定同一份 ImPad debug APK：SHA-256 `d080b4fc0781c3491db0238fc9d285abd7f4b554e55c24e1f959c65b5004eddb`，构建时源码树 SHA-256 `6cf287e29320ef9f44239c5d0e08793517d208cdae6666a60f295536e6f79cf3`。QQ 与企微 production verifier 均为 `VERIFIED`，固定代表回归均为 `PASSED`。QQ gate 观察到模块开关下手机/平板 AppID 的差异及 QFix、DexKit native 加载；企微 gate 观察到平板二维码页、三个 hook、Tinker ready 和 DexKit native 加载。

原始运行材料保存在本地证据归档，未随仓库提交或远程归档。本页保留应用 APK、模块 APK、源码树身份和已验证范围摘要；不提供指向本机归档的链接。Linux x86_64 静态检查和回归也属于对应历史源码身份，不代表当前源码状态。

QQ gate 观察到模块关闭与启用时的手机/平板 AppID 差异，以及该构建的 QFix 和 DexKit native 加载成功。企微 gate 观察到模块开启后平板二维码页稳定显示，以及该构建的三个 hook、Tinker ready 和 DexKit native 加载成功。测试不使用真实账号、不扫描二维码、不提交登录；结果不能证明真实账号登录、服务端接受或双设备会话共存。QFix/Tinker 观察结果只适用于表中精确 APK，不保证其他热补丁版本或所有 Android 设备。

微信 8.0.69 / 3040 已接入共享 RULES checker/runtime，并以独立 acceptance 记录固定其规则预期。统一迁移候选模块 SHA-256 为 `35aed4a85250f45256185341cd25b559ac03b08ebe69de75d0b9806200e73979`；该模块对微信 8.0.69 / 3040、QQ 9.3.1 / 14378、企业微信 5.0.8 / 70675 的固定静态回归均通过。该模块的三应用新运行验收尚未完成，不能把此静态结果当作运行通过或扩大运行支持范围。上文 QQ/企微运行结果仍只绑定其明确列出的历史模块 APK 与源码身份；其他构建不能由相邻版本结果推定通过。

该统一模块在 Oracle ARM64 ReDroid 的四轮运行尝试未通过；最新 QQ 运行因 collector 无法证明 socket owner 而被 production verifier 拒绝。曾在其他运行中成功的精确 binary/APK 复测在 Oracle 也未通过。Oracle framework 文件与权限一致性尚未核实，环境或部署差异仍待定位，因此当前材料不能断定这是纯宿主问题或规则缺陷。后续将以同一冻结模块在已知成功的 GitHub-hosted ARM64 ReDroid 路径继续验收；结果尚未产生。

## 历史兼容记录

以下表格保留迁移前 target/profile 的身份、状态和 ImPad 0.1.0 运行记录，作为历史资料。三个应用的旧 `targets.json` 均不参与当前 APK 打包、checker、runtime 或流水线。旧记录不能替代当前 acceptance 规则预期，也不代表所有同版本构建均适用。

历史 `runtime-verified-local` 表示旧 profile 的本地运行验证；`runtime-verified-hosted` 表示旧 profile 的托管运行验收。两者均采用无账号、不扫码、不提交登录的验证设计。

## 微信

包名：`com.tencent.mm`

| 版本 | versionCode | APK SHA-256 | profile 验证状态 | 0.1.0 release 运行验收 |
| --- | --- | --- | --- | --- |
| 8.0.69 | 3040 | `e4da5809f2b3305e375db38897a4082085adc6b51182fa807813c4dda1c3a948` | `runtime-verified-local` | 未重跑 |
| 8.0.79 | 3200 | `5feb100337981467fd257c3ad66bb171f54a69d2579b2ecc70d5a628db8e7282` | `runtime-verified-hosted` | 通过 |
| 8.0.79 | 3200 | `f2a725bc04302ad218a563533ae79356e66b64aa711b275bc919a8d432f7e15d` | `runtime-verified-hosted` | 通过 |

签名证书 SHA-256：`0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c`

历史 targets 与官方来源：[compatibility/wechat/targets.json](../compatibility/wechat/targets.json)。此文件只用于追溯迁移前记录。

## QQ

包名：`com.tencent.mobileqq`

| 版本 | versionCode | APK SHA-256 | profile 验证状态 | 0.1.0 release 运行验收 |
| --- | --- | --- | --- | --- |
| 9.3.1 | 14378 | `ccf9714d268662ee8311add537705e66dee3367d45a20eabbf57b0070949595e` | `runtime-verified-hosted` | 未重跑 |
| 9.3.70 | 16410 | `e8c28713b740c18246989d47916d9fbbad06d440778a5aab3f4e108a2423e55c` | `runtime-verified-hosted` | 通过 |

签名证书 SHA-256：`ea6e97ad6c34f7039a9c6daba732c97d0e098e83ede2b4d52c76eb0184ac7a38`

历史 profile 与官方来源：[compatibility/qq/targets.json](../compatibility/qq/targets.json)。该文件保留作历史资料，不作为当前运行时准入。

## 企业微信

包名：`com.tencent.wework`

| 版本 | versionCode | APK SHA-256 | profile 验证状态 | 0.1.0 release 运行验收 |
| --- | --- | --- | --- | --- |
| 5.0.8 | 70675 | `59b3e620a3cf1d1f8740ea320e05fd821bb811040b36f69aa307e7fc8a2ca26d` | `runtime-verified-local` | 未重跑 |
| 5.0.12 | 83835 | `6b3cead315fa7d8c302b4b538f25abe5afc935b69f9cd49ee212a780f6afdf40` | `runtime-verified-hosted` | 通过 |

签名证书 SHA-256：`66896ae0aae48b9e963a3e034fa2ca813ca2f661f53619a92263a8a5c2e3f388`

历史 profile 与官方来源：[compatibility/wecom/targets.json](../compatibility/wecom/targets.json)。该文件保留作历史资料，不作为当前运行时准入。

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
