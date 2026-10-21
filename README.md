# I’m Pad / ImPad

ImPad 是通过 LSPosed 为微信、QQ 和企业微信提供平板登录界面适配的 Android 模块。

## 历史验证基准

QQ 9.3.1（14378）和企业微信 5.0.8（70675）的指定官方 APK 曾通过静态规则检查、固定代表回归和无账号 ARM64 ReDroid 运行 gate。该次验证绑定 ImPad debug APK SHA-256 `d080b4fc0781c3491db0238fc9d285abd7f4b554e55c24e1f959c65b5004eddb` 与 140 项构建输入的源码树 SHA-256 `6cf287e29320ef9f44239c5d0e08793517d208cdae6666a60f295536e6f79cf3`。

该次 QQ gate 观察到模块关闭与启用时手机/平板 AppID 的差异，以及 QFix 和 DexKit native 加载成功。企微 gate 观察到模块关闭时没有平板入口、启用后平板二维码页稳定显示，以及三个 hook、Tinker ready 和 DexKit native 加载成功。production verifier 为 `VERIFIED`，固定代表回归为 `PASSED`。这些结论只适用于上面列出的应用 APK、模块 APK 和源码身份，不代表当前工作树或其他构建已通过相同运行验收。原始运行材料保存在本地证据归档，未随仓库提交或远程归档；本页保留可携带的 APK/module 身份和验证范围摘要。

微信 8.0.69 / 3040 有独立的 RULES 验收记录；该记录不替代上述旧模块绑定的 QQ/企微运行 gate，也不能推定其他 APK 或模块构建通过。遇到应用更新或行为异常时，请核对[已验证版本与范围](docs/support.md)。

微信规则统一迁移已通过静态构建和固定代表回归。当前候选模块 SHA-256 为 `35aed4a85250f45256185341cd25b559ac03b08ebe69de75d0b9806200e73979`；它对微信 8.0.69 / 3040、QQ 9.3.1 / 14378 和企业微信 5.0.8 / 70675 的静态回归均通过。该模块尚未完成新的三应用运行验收，因此不据此扩大运行支持范围。Oracle ARM64 ReDroid 的四轮尝试未通过，运行环境差异仍待定位；GitHub-hosted ARM64 ReDroid 验收尚未完成。详见[验证范围](docs/support.md)。

## 安装与使用

- Android 9 或更高版本及已 Root 的设备。
- 已安装并正常运行 LSPosed。

1. 安装 ImPad APK。
2. 在 LSPosed 中启用 ImPad，并为要使用的目标应用勾选作用域。
3. 强制停止目标应用后重新打开。

模块没有独立启动界面或配置页面。遇到应用更新或行为异常时，先核对当前验收范围和[历史记录说明](docs/support.md)。

## 指定官方 APK 验证或适配

在 GitHub Actions 中运行 **Run app compatibility pipeline**，选择 `app_id` 和 `operation`，并可填写该应用官方来源的精确 `apk_url`。留空时流水线按该应用的官方发现策略选择候选。填写 URL 后会检查指定 APK；不会换成官网最新版。

- `verify` 运行候选静态检查、固定代表回归和独立运行验收；不调用模型、不创建 PR，也不写入正式验收登记。
- `adapt` 允许在候选确实需要适配时提出新规则。新规则使用新的不可变 ID 和候选临时路由，然后回到相同的静态、回归、构建和独立运行 gate；通过前不会成为正式支持。

本地精确候选准备命令及其边界见[开发与兼容性维护](docs/development.md)。

## 验收边界

无账号验收不输入真实账号、不扫码、不提交登录，因此不证明真实账号登录、服务端接受或双设备会话共存。当前验收也不代表所有 Android 设备均通过，不承诺完整热补丁适配。

## 许可证

本项目采用 [GPL-3.0-only](LICENSE)。私下修改并使用无需发布修改版；分发修改版时须按许可证要求提供对应源码并继续使用 GPL v3 授权，参见 [GNU GPL FAQ](https://www.gnu.org/licenses/gpl-faq.en.html#GPLRequireSourcePostedPublic)。

第三方组件的许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。开发、构建与候选验证说明见[开发文档](docs/development.md)。
