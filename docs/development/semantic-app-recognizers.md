# 三应用语义识别器

## 范围与合同

三个应用已提供统一 SemanticRuleCatalog，正式模块入口仍使用原流程，须等缓存、批次安装、checker/报告和验收迁移一同完成后切换。

QQ 识别含 TABLET 枚举及缓存关系的设备 Getter；企微必须同时找到设备类型、Application 和 Service 的三个本地判断。微信在每个相关声明命名空间枚举 decision → wrapper → login 的完整关系，tablet 目标统一为已证明的 wrapper。直接转发、缓存包装与缓存重读属于同一行为合同，不按版本、包渠道、补丁路径或补丁哈希决定规则编号。

引用关系同时比较 descriptor 和调用者命名空间内的来源。两个 tablet 候选只有一个完整登录关系时可以选择；两个不同完整关系冲突。控制流预算耗尽返回 Incomplete，不能因另一个候选成功而放行。

微信 tablet 回调仍执行原方法，并在聊天页栈之外返回 true；login 回调仍执行原方法后恢复被隐藏的入口。真实补丁的 wrapper 带有布尔标志触发的强制 true 旁路：仅接受结构上能证明该旁路写入常量 1，并跳到缓存/决策转发共用 RETURN 的形态。其他提前返回、错误寄存器或非 true 旁路拒绝。该约束不绑定标志字段名称。

## 真实输入证据

2026-10-27 使用本地官方签名 APK、固定代表和本次手机已保存的合并 DEX，在独占只读 API 37 arm64/16 KB AVD 运行原生解析与反射绑定探针；没有访问用户手机。

| 输入 | tablet / device_type 目标 | 结果 |
| --- | --- | --- |
| 微信 3040 固定代表 | `com.tencent.mm.ui.qj.A()` | Selected，两个角色绑定成功 |
| 微信 3200 固定代表 | `com.tencent.mm.ui.ok.B()` | Selected，两个角色绑定成功 |
| 微信 3200 手机 base | `com.tencent.mm.ui.ok.B()` | Selected，两个角色绑定成功 |
| 微信 3200 合并补丁在前、base 在后 | `com.tencent.mm.ui.ok.B()` | Selected，两个角色绑定成功 |
| 微信 Play 3182，含三个 split | `com.tencent.mm.ui.ok.B()` | Selected，两个角色绑定成功 |
| QQ 14378 | `com.tencent.common.config.pad.PadUtil.a(Context)` | Selected，TABLET Getter 绑定成功 |
| 企微 70675 | 原三个本地判断 | Selected，三个角色绑定成功 |

最终纠正入口控制流后的证据为 `work/effective-code-feasibility/real-input-avd-20261027-07/summary.json`，精确输入矩阵为同级 `real-input-matrix.json`。每次运行冻结探针和模块字节、输入/依赖哈希、SDK 签名与包身份以及原始命令输出。较早的 run06 保留了过度收紧入口检查导致补丁 NoMatch 的反证，不计作通过。

这些是原生解析和绑定证据，不证明 Hook 业务效果、无账号 UI 路径、完整签名 Tinker 补丁重放或 ART 驻留 Method 的来源。当前只保存了合并 DEX，原始完整补丁包缺失，不能将其改称完整补丁验收。

## 捕获成本

将归档中的 DEX 规范化为 STORED 且数据对齐的单 DEX ZIP，避免逐 DEX 解压后再次压缩。run04 和 run05 的七组选择身份及反射绑定完全相同；单次 AVD 捕获从约 6.3–13.1 秒降为 0.69–2.10 秒。两次均为独立解析探针，不能当作最终模块启动、热缓存或跨设备性能保证。最终发布/R8 包仍需测首次解析、缓存命中与峰值内存。

独立审查发现缓存包装入口旁路问题；helper 与真实 DEX 关系测试先复现，再分别验证拒绝 false 旁路和接受已证明的 true 旁路。正式入口切换前继续核对完整最终验收链。
