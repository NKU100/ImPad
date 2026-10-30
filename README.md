# I’m Pad / ImPad

ImPad 是一个 LSPosed 模块，为微信、QQ 和企业微信适配平板登录界面。模块没有独立界面，启用后会在目标应用中生效。

## 下载

- [ImPad 最新版](https://github.com/NKU100/ImPad/releases/latest)
- [LSPosed 模块仓库最新版](https://github.com/Xposed-Modules-Repo/io.github.nku100.impad/releases/latest)

请从上述官方来源获取安装包。更新内容可查看[发布说明](https://github.com/NKU100/ImPad/releases)。

## 安装与升级

设备需运行 Android 9 或更高版本，使用 ARM64（`arm64-v8a`）架构并已 Root，且已安装并正常运行支持 modern 模块的 LSPosed。

1. 安装 ImPad APK。
2. 在 LSPosed 中启用 ImPad，并为需要适配的应用勾选作用域。
3. 强行停止目标应用，再重新打开。

升级时从官方来源下载新版并安装，然后检查 LSPosed 中的模块状态和作用域，再强行停止并重开目标应用。若安装时出现签名冲突，请先核对安装包来源和设备上已有版本。

## 常见问题

**模块没有图标或独立界面？**

这是正常情况。ImPad 通过 LSPosed 在目标应用中运行，请到 LSPosed 启用模块并设置作用域。

**启用后没有效果？**

检查模块是否已启用、目标应用是否在作用域内，并强行停止后重新打开应用。应用升级或热更新可能影响适配效果。

## 反馈

请在 [GitHub Issues](https://github.com/NKU100/ImPad/issues) 提交问题，并附上目标应用名称与版本、ImPad 版本、设备 Android 版本、具体现象及相关日志。请勿上传账号凭据、二维码或令牌。

## 许可证

本项目采用 [GPL-3.0-only](https://github.com/NKU100/ImPad/blob/main/LICENSE)。第三方组件的许可证见 [第三方许可证](https://github.com/NKU100/ImPad/blob/main/THIRD_PARTY_NOTICES.md)。

开发、构建与维护说明见[开发文档](https://github.com/NKU100/ImPad/blob/main/docs/development.md)。
