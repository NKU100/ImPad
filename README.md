# I’m Pad / ImPad

ImPad 是通过 LSPosed 为微信、QQ 和企业微信提供平板登录界面适配的 Android 模块。

模块通过共享规则识别目标应用中的平板登录入口，具体已验证版本见[已验证版本与范围](docs/support.md)。应用更新后需要重新验证兼容性。

## 安装与使用

- Android 9 或更高版本、ARM64（`arm64-v8a`）且已 Root 的设备。
- 已安装并正常运行 LSPosed。

1. 安装 ImPad APK。
2. 在 LSPosed 中启用 ImPad，并为要使用的目标应用勾选作用域。
3. 强制停止目标应用后重新打开。

模块没有独立启动界面或配置页面。遇到应用更新或行为异常时，先核对当前[验收范围](docs/support.md)。

## 指定官方 APK 验证或适配

在 GitHub Actions 中选择 **Check WeChat compatibility**、**Check QQ compatibility** 或 **Check WeCom compatibility** 对应应用入口，再选择 `operation`。可填写该应用官方来源的精确 `apk_url`；留空时流水线按该应用的官方发现策略选择候选。填写 URL 后会检查指定 APK；不会换成官网最新版。`module_run_id` 和 `module_artifact` 是可选参数，仅用于 `verify`；留空时使用默认构建。

- `verify` 运行候选静态检查、固定代表回归和独立运行验收；不调用模型、不创建 PR，也不写入正式验收登记。
- `adapt` 允许在候选确实需要适配时提出新规则。新规则使用新的不可变 ID 和候选临时路由，然后回到相同的静态、回归、构建和独立运行 gate；通过前不会成为正式支持。

本地精确候选准备命令及其边界见[开发与兼容性维护](docs/development.md)。

## 验证范围

无账号验收验证模块加载与平板登录入口，不证明真实账号登录、服务端接受或双设备会话共存。具体测试环境、结果与限制见[验证记录](docs/validation/runtime-feature-matching-final.md)。

## 许可证

本项目采用 [GPL-3.0-only](LICENSE)。私下修改并使用无需发布修改版；分发修改版时须按许可证要求提供对应源码并继续使用 GPL v3 授权，参见 [GNU GPL FAQ](https://www.gnu.org/licenses/gpl-faq.en.html#GPLRequireSourcePostedPublic)。

第三方组件的许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。开发、构建与候选验证说明见[开发文档](docs/development.md)。
