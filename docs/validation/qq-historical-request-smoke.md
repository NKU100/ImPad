# QQ 老版本请求身份实验

> 历史实验记录：保留当时的观察和局限；现行自动验收使用 [ReDroid 运行环境](../environment/redroid-runtime.md)，本记录不代表当前通过状态。

2026-10-13 至 2026-10-14，在复用 AVD `ImPadRuntimeSmoke` 上测试腾讯官方 ARM64 APK。未输入账号或密码，未扫码或提交登录。

## 有效结果

| QQ | versionCode | 模块关闭请求头 AppID | 模块开启请求头 AppID | 同版本对照命令 |
| --- | ---: | ---: | ---: | --- |
| 9.1.95 | 10460 | 537297350 | 537297389 | SsoEstablishShareKey |
| 9.2.10 | 11310 | 537309838 | 537309877 | SsoSecureAccess |
| 9.2.15 | 11480 | 537312176 | 537312215 | SsoSecureAccess |

两侧值均匹配各 APK Manifest。三个 APK 的 AppID Getter 都是 `AppSetting.f()I`，设备类型 Getter 为 `PadUtil.a(Context)->DeviceType`；这些精确描述符只写入临时实验模块。

报文观察器没有调用 QQ Getter，没有版本分支。其 SHA-256 始终为 `c389f21608e1e6a03340060b9bef107404d147899a0bfd81c60424cc838c376e`，与之前测试 9.3.1、9.3.70 的严格解析版本一致。命令按协议字段布局解析，观测值来自实际发送数据。

## 来源和证据

- [9.1.95 官方 APK](https://downv6.qq.com/qqweb/QQ_1/android_apk/Android_9.1.95_64.apk)
- [9.2.10 官方 APK](https://downv6.qq.com/qqweb/QQ_1/android_apk/Android_9.2.10_64.apk)
- [9.2.15 官方 APK](https://downv6.qq.com/qqweb/QQ_1/android_apk/Android_9.2.15_64.apk)

三个包均通过 apksigner 验证，证书 SHA-256 为 `ea6e97ad6c34f7039a9c6daba732c97d0e098e83ede2b4d52c76eb0184ac7a38`。APK 哈希及版本身份见 `work/qq-history-smoke/identities.json`，下载来源见 `downloads.json`。

权威结果为 `work/qq-history-smoke/matrix-valid.json`。原始有效采集为各版本目录下的 `valid-disabled.json`、`valid-enabled.json`；9.2.15 的关闭证据使用 `valid-disabled-repeat.json`。同目录包含 Manifest、方法定义、签名报告和加载日志。

## 排除的采集与环境条件

早期导航停在老版“其他方式登录”面板，随后按可见的“QID/邮箱登录”入口补充导航。导航发生在实验控制脚本中，协议观察器未修改。指定命令缺失时不计通过，允许重新采集；不会用日志上传替代安全命令。

连续安装遇到空间不足，改为保留 QQ 数据的卸载/重装。LSPosed 会清除被卸载 QQ 的作用域，模块总开关开启不代表 QQ 被注入。作用域缺失的早期采集作废，不能作为模块行为结论。

有效轮次每次重装 QQ 后重新安装实验模块，由官方 Manager 重建静态声明作用域。采集前独立读取数据库核验 QQ 作用域，再切换模块状态、停止并启动 QQ，检查安装 APK 哈希。重启后设置 16 KB 兼容参数。早期 `matrix.json`、`matrix-final.json` 不是有效验收结果。

## 判据边界

这里只证明自然发送的 O3 安全请求头使用对应手机/平板客户端 AppID，不证明账号认证、服务端会话身份或手机共存。加上之前的 9.3.1、9.3.70，目前有五个版本的协议解析与身份切换证据，不能据此保证未来所有版本稳定。

自动晋级继续关闭。生产化前仍需固定导航和预热流程，补齐流重组、出口覆盖、异常样本与验收不可用状态。验收器不能由候选适配修改通过条件。

实验结束恢复 QQ 9.3.1、原模块和声明作用域。正式规则未加入这些老版本，普通构建产物也已按原规则重新生成。修改未提交或推送。
