# QQ 兼容性与运行验证

QQ 复用 ImPad 的官方版本发现、静态检查、Codex 自动适配、旧版回归和独立运行验收流水线。微信和企微仍使用各自现有的界面行为验收。

## 适配目标

模块仅在 `com.tencent.mobileqq:MSF` 安装设备类型 Hook，使 QQ 选择 TABLET 设备类型。兼容性清单只声明 `device_type`，不声明、调用或 Hook AppID Getter。

初始注册基线为官方 QQ 9.3.1（versionCode 14378），状态为 `static-verified`。APK SHA-256 为 `ccf9714d268662ee8311add537705e66dee3367d45a20eabbf57b0070949595e`。最新版必须经过候选检查与适配流程，不能直接登记为已支持版本。历史版本的实验记录不等同于注册支持。

## 独立运行验收

可信控制器从精确候选 APK Manifest 的 `AppSetting_params` 与 `AppSetting_params_pad` 提取手机和平板 AppID，并绑定 APK SHA-256。预期值由控制器生成，不能由候选规则自行提供。

控制器依次关闭、开启模块，重启 QQ，在 MSF 进程观察自然发生的原生 TCP 发送。观察器不调用 QQ Java 接口，不替换发送内容。固定解析器读取未登录 SSO 请求头的 AppID，仅接受以下安全初始化命令：

- `trpc.o3.ecdh_access.EcdhAccess.SsoSecureAccess`
- `trpc.o3.ecdh_access.EcdhAccess.SsoEstablishShareKey`

两侧必须至少出现一个相同的固定命令。所有被接受的请求分别匹配手机和平板 AppID，不能挑选一条正确请求忽略矛盾请求。控制器还检查安装 APK、模块 APK、观察器哈希、模块开关、QQ scope、采集完整性及进程身份。

观察器固定 Frida 版本及服务端归档摘要。原始发送字节只在内存中用于解析，不落盘；证据只保存命令、AppID、序号和验收元数据，不保存账号、票据或请求正文。每次结束均清理临时服务、端口转发并恢复模块开启。

缺失请求、观察器断开、残留半帧或环境异常产生 `ACCEPTANCE_UNAVAILABLE`；完整证据中身份不匹配产生 `RUNTIME_REJECTED`，均不能晋级。只有独立请求对照和模块诊断满足要求，才产生 `RUNTIME_SMOKE_VERIFIED`，验收类型为 `REQUEST_APP_ID_CONTRAST`。独立最终验收通过后才能晋级 `RUNTIME_VERIFIED`。

这证明指定未登录安全请求使用了平板身份，不证明账号登录成功、服务端接受平板登录或手机与平板会话共存。自动化只导航登录方式，不输入账号凭据、不扫码。

## 自动适配边界

`compatibility-qq.yml` 使用官方 ARM64 APK 来源。Codex 接收的目标是让候选 QQ 的自然安全请求在模块关闭时使用手机 AppID、开启时使用平板 AppID，同时保持已注册版本回归通过。

Codex 可以修改 QQ 的设备类型适配及候选描述符；可信观察器、协议解析器、验收条件和 Manifest 预期值由控制器掌握。若未来版本改变网络协议，现有验收应失败或不可用，需单独审查验收协议，不能由本轮候选适配自行放宽。

手动运行使用 `runtime-smoke.yml` 的 `app_id=qq`。本地复用现有 AVD；真实登录验收由人工完成。
