# I’m Pad / ImPad

[![LSPosed 仓库](https://img.shields.io/badge/LSPosed-%E6%A8%A1%E5%9D%97%E4%BB%93%E5%BA%93-blue)](https://github.com/Xposed-Modules-Repo/io.github.nku100.impad) [![最新版](https://img.shields.io/github/v/release/NKU100/ImPad?label=%E6%9C%80%E6%96%B0%E7%89%88)](https://github.com/NKU100/ImPad/releases/latest) [![许可证](https://img.shields.io/github/license/NKU100/ImPad)](https://github.com/NKU100/ImPad/blob/main/LICENSE)

[![构建](https://img.shields.io/github/actions/workflow/status/NKU100/ImPad/build.yml?branch=main&event=push&label=%E6%9E%84%E5%BB%BA&logo=github)](https://github.com/NKU100/ImPad/actions/workflows/build.yml) [![微信适配](https://img.shields.io/github/actions/workflow/status/NKU100/ImPad/compatibility-wechat.yml?branch=main&label=%E5%BE%AE%E4%BF%A1%E9%80%82%E9%85%8D&logo=github)](https://github.com/NKU100/ImPad/actions/workflows/compatibility-wechat.yml) [![QQ适配](https://img.shields.io/github/actions/workflow/status/NKU100/ImPad/compatibility-qq.yml?branch=main&label=QQ%E9%80%82%E9%85%8D&logo=github)](https://github.com/NKU100/ImPad/actions/workflows/compatibility-qq.yml) [![企微适配](https://img.shields.io/github/actions/workflow/status/NKU100/ImPad/compatibility-wecom.yml?branch=main&label=%E4%BC%81%E5%BE%AE%E9%80%82%E9%85%8D&logo=github)](https://github.com/NKU100/ImPad/actions/workflows/compatibility-wecom.yml)

以上状态展示流水线最近运行情况，不代表所有应用版本均兼容。

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
