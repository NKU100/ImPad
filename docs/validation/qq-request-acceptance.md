# QQ 独立请求验收可行性

> 历史实验记录：保留当时的观察和局限；现行自动验收使用 [ReDroid 运行环境](../environment/redroid-runtime.md)，本记录不代表当前通过状态。

2026-10-13，在复用的 `ImPadRuntimeSmoke`（emulator-5572）上检查 QQ 9.3.1。未输入账号或密码，未提交登录、发短信或扫码。

## 验收策略

Getter 由版本候选描述符选择，适配方可能选择一个返回值正确但不被登录消费的方法。因此 Getter 对照只记录 `CONFIGURATION_SMOKE_VERIFIED`，不授予 `RUNTIME_VERIFIED`。受保护控制器对 QQ 返回 `STATIC_VERIFIED_PENDING_RUNTIME` 和 `runtimeConclusion=acceptance-unavailable`，不修改候选验证等级、不自动晋级。旧版清单降为 `static-verified`；历史配置报告保留原样。

诊断脚本成功退出只表示采集成功，不代表运行验收通过。微信、企微仍使用原来的运行验收入口。

## 本轮实际观测

临时 Frida 观察器附加 QQ 主进程和 MSF，拦截系统 `send`、`sendto`、`write`、`writev`，第二轮增加 `sendmsg`。观察器不调用 QQ Getter、不替换返回值、不改变发送内容。仅记录进程、出口、socket 类型、发送长度、返回长度，以及两个 APK AppID 的四字节大小端搜索结果；没有保存请求内容、账号或票据。它是可行性实验，不是生产协议解析器，未覆盖所有网络发送路径。

第一轮点击登录后进入手机号页面；采集到的事件只有本地 socket 活动。第二轮进入账密登录页，主进程和 MSF 均报告 observer-ready，随后采集到 12 次 TCP/TCP6 发送事件。四字节搜索没有命中手机或平板 AppID。发送事件不能证明发生了登录请求，字节搜索未命中不能证明报文没有身份字段。没有进行模块关闭/开启的请求身份对照，因为当前尚无法识别应比较的登录命令和身份字段。

原始材料在 `work/qq-request-spike/`：`socket-events.jsonl`、`password-page-socket-events.jsonl`、UI XML、临时观察器。临时服务和端口转发已停止，QQ 留在账密页，模块配置未变更。

## 协议参考与边界

[Lagrange.Core v1 SsoPacker](https://github.com/LagrangeDev/Lagrange.Core/blob/v1/Lagrange.Core/Internal/Packets/SsoPacker.cs) 将 SubAppId 写入 SSO 请求头；[ServicePacker](https://github.com/LagrangeDev/Lagrange.Core/blob/v1/Lagrange.Core/Internal/Packets/ServicePacker.cs) 展示外层加密处理。这些是协议实现参考，没有证明其解析器适用于当前 Android QQ。

可继续研究系统出口数据的协议识别、TCP 流重组及独立解码，先确认存在无需凭据即可触发且携带身份的登录命令，再建立同一 APK 模块关闭/开启的对照。观察器与协议解析规则由验收侧独立维护，不能允许候选适配自行选择观测内部 Getter 或修改通过条件。

本轮结论为“尚未建立无账号独立身份验收”，不是“QQ 适配失败”，也不是“该验收不可能”。服务端接受登录及手机会话共存仍需真实账号与另一台已登录设备。

## 后续报文解码实验

同日继续在 QQ 9.3.1 做模块关闭/开启对照。通过官方 LSPosed Manager 切换模块，每次停止 QQ 并从 SplashActivity 启动，再进入登录页及账密页。观察器只拦截系统 socket 发送出口，不调用宿主内部 Getter。按进程和 fd 拼接发送数据，尝试识别长度帧、协议 12 和认证标记 2，用固定零密钥进行 QQ TEA 解密，并检查末尾七字节填充及 SSO 头边界。

两次均解出 `trpc.o3.ecdh_access.EcdhAccess.SsoSecureAccess` 命令。关闭模块的一帧请求头 AppID 为 `537360567`；开启模块的四帧请求头 AppID 均为 `537360606`。此外解出日志上传请求，另有扩展协议及其他流量未解析。未保存完整报文，原始字节只在进程内用于解码；结果文件只保存出口事件、协议、命令和身份字段等元数据。

这证明当前 APK 的实际发送报文头存在独立于 Getter 的手机/平板身份对照。它尚未证明该命令中的身份等同于账号认证参数，也未验证响应或服务端接受。没有真实账号登录，不代表双设备共存通过。

结果位于 `work/qq-request-spike/wire-disabled.json`、`wire-enabled.json`、`wire-contrast.json`；实验脚本为 `wire_probe.py`、`contrast.py`。模块已恢复开启，临时服务和端口转发已清理。

协议解析目前只是一份可行性原型：未覆盖 fd 复用、所有出口、扩展帧或跨版本协议变化；命令采用请求头中有长度前缀的字符串识别，仍需严格字段布局验证。将其升级为生产验收器前，应验证登录语义、覆盖另一版本及手机/平板固定报文样本，并从精确 APK Manifest 导出期望值。此次没有开放自动晋级。

## 字段语义与跨版本复测

后续使用同一份观察器（SHA-256 `c389f21608e1e6a03340060b9bef107404d147899a0bfd81c60424cc838c376e`）验证 QQ 9.3.1 和 9.3.70。命令识别改为从固定 SSO 头布局顺序读取长度字段，检查边界和 ASCII 命令；不再在头内搜索字符串。该解析器没有版本分支，也没有调用 QQ 内部方法。

| APK | 同版本对照命令 | 关闭模块请求头 AppID | 开启模块请求头 AppID |
| --- | --- | --- | --- |
| 9.3.1 | SsoEstablishShareKey | 537360567 | 537360606 |
| 9.3.70 | SsoSecureAccess | 537395438 | 537395477 |

期望值分别从各 APK Manifest 提取。新版首次关闭采集到建共享密钥命令，重复关闭后采集到安全访问命令，才与开启结果构成同命令对照。恢复旧版后的有效复测，两种状态均捕获建共享密钥命令。首次初始化与缓存状态会影响命令选择，不能把任意网络发送当作指定命令出现。

字段在此称为 `packetAppId`：它是实际 SSO 请求头的客户端 AppID，不是已证明的登录业务 subappid/bid 或服务端会话设备类型。旧版反编译的 `com.tencent.mobileqq.dt.web.O3BusinessHandler` 属于 `qq-devicetoken-impl_release`，使用安全事件分发，并将 Servlet 的 `withouLogin` 设为 true；命令清单包括建共享密钥和安全访问。该证据说明它是可以未登录运行的设备令牌/安全请求通道，不能据此等同账号认证成功。

新版实验使用含候选精确描述符的临时模块。构建结束恢复原规则文件；新版没有留在正式清单。实验结束恢复 QQ 9.3.1 和原模块，设备上的 APK 哈希分别为 `ccf9714d268662ee8311add537705e66dee3367d45a20eabbf57b0070949595e`、`07d9ef91d8a5af4c55b66d3f3647a30c137c04825eef2923b0e9720d8b3df234`，模块保持开启。恢复时遇到空间不足，以保留数据的卸载/重装恢复旧 APK；重启后重新设置 16 KB 兼容参数，再完成有效采集。此前模块未加载及启动失败的采集不计入对照。

汇总为 `work/qq-request-spike/cross-version-contrast.json`，严格解析的旧版样本为 `wire-9.3.1-disabled-strict.json`、`wire-9.3.1-enabled-strict.json`，新版同命令样本为 `wire-9.3.70-disabled-repeat.json`、`wire-9.3.70-enabled.json`。临时服务及转发已清理。

结论：协议观察器跨这两个版本可复用，实际安全请求头的手机/平板身份切换已观察到。生产验收仍需固定预热与命令契约、补齐流重组/出口覆盖及异常样本，并明确只能授予“请求身份冒烟”级别。自动晋级继续关闭，账号认证、服务端接受、手机共存未验证。

## 原有检查记录

针对 QQ 策略、运行结果晋级和公共冒烟的 35 项检查通过，`git diff --check` 通过。修改仍在本地，未提交或推送。
