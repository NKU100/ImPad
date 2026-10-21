# I’m Pad / ImPad

ImPad 是通过 LSPosed 为微信、QQ 和企业微信提供平板登录界面适配的 Android 模块。

## 已验证范围

微信 8.0.69（3040）、QQ 9.3.1（14378）和企业微信 5.0.8（70675）已使用同一份模块 APK，通过 GitHub-hosted ARM64 ReDroid 运行验收。模块 APK SHA-256 为 `d8cdaf1aba2d19503c9a9a2679195456657faae1b5fae8e9d52e2c5ba1b88691`，构建源码提交为 `8690ed5162ba71c1d756f9e57793c774f2501de7`，127 项模块源码树 SHA-256 为 `654e389fd9b35a1362d6d9c79d2f07f8cd249f141fdfff59c74cff1b70f25aae`。

三次独立运行均通过应用验收、运行冒烟、tooling 4/4 和 production verifier。对应的 [QQ 9.3.1](https://github.com/NKU100/ImPad/actions/runs/39555436990)、[微信 8.0.69](https://github.com/NKU100/ImPad/actions/runs/39556863774) 和[企业微信 5.0.8](https://github.com/NKU100/ImPad/actions/runs/39557803980) 验收均使用无账号流程：未登录真实账号、未扫码、未提交登录。具体行为与边界见[已验证版本与范围](docs/support.md)。

这些结果只适用于表列的应用 APK 和精确模块构建。验收观察到 QQ QFix、微信和企微 Tinker 就绪，以及各自流程中的 native 加载；这不代表完整热补丁行为或所有设备、版本均已覆盖。QQ 采集沿用 SYN 阶段 owner 缓存合同；FD handoff 未纳入验证范围。

## 安装与使用

- Android 9 或更高版本及已 Root 的设备。
- 已安装并正常运行 LSPosed。

1. 安装 ImPad APK。
2. 在 LSPosed 中启用 ImPad，并为要使用的目标应用勾选作用域。
3. 强制停止目标应用后重新打开。

模块没有独立启动界面或配置页面。遇到应用更新或行为异常时，先核对当前[验收范围](docs/support.md)。

## 指定官方 APK 验证或适配

在 GitHub Actions 中运行 **Run app compatibility pipeline**，选择 `app_id` 和 `operation`，并可填写该应用官方来源的精确 `apk_url`。留空时流水线按该应用的官方发现策略选择候选。填写 URL 后会检查指定 APK；不会换成官网最新版。

- `verify` 运行候选静态检查、固定代表回归和独立运行验收；不调用模型、不创建 PR，也不写入正式验收登记。
- `adapt` 允许在候选确实需要适配时提出新规则。新规则使用新的不可变 ID 和候选临时路由，然后回到相同的静态、回归、构建和独立运行 gate；通过前不会成为正式支持。

本地精确候选准备命令及其边界见[开发与兼容性维护](docs/development.md)。

## 验收边界

无账号验收不证明真实账号登录、服务端接受或双设备会话共存。当前验收不代表所有 Android 设备均通过，也不覆盖未观测的完整热补丁变化或 QQ socket FD handoff。

## 许可证

本项目采用 [GPL-3.0-only](LICENSE)。私下修改并使用无需发布修改版；分发修改版时须按许可证要求提供对应源码并继续使用 GPL v3 授权，参见 [GNU GPL FAQ](https://www.gnu.org/licenses/gpl-faq.en.html#GPLRequireSourcePostedPublic)。

第三方组件的许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。开发、构建与候选验证说明见[开发文档](docs/development.md)。
