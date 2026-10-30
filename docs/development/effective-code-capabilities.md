# 活动代码来源能力验证

## 结论

当前锁定 DexKit 可读取部分活动 DEX 内容，但未提供实际已定义 Class/Method 到该 DEX 的可验证关联。严格按原设计的来源证明合同，现有依赖不足以直接开始正式输入提供器实施；用户随后确认收窄保证，新的启动快照实现与验证见 [启动代码快照与查询基础](startup-code-snapshot.md)。

这是来源证明能力不足，不代表单 DEX 或普通应用不能扫描，也不代表已确认的语义选择方向失效。报告中的 `UNSUPPORTED` 专指“未达到严格来源证明合同”；十个实验用例均执行成功，没有用错误扫描结果认证运行来源。

用户已确认采用以下保证范围：把首版限定为受支持加载器在应用启动就绪时的稳定代码代次；按每个活动 DEX 分别捕获内容，并显式处理加载顺序、整类覆盖、实际声明类加载器和可观察代次变化；对既有同名类驻留、未知注入或来源不明情况明确拒绝。这仍需要验证可识别这些前提的能力，不能声称已解决。该选择明确收窄原草案的强保证；快照不认证已驻留 Class/Method 的 ART 来源。版本提示、完整语义方案与冲突拒绝方向保持不变。

另一条路径是增加针对 ART 的 Class/Method 来源支持；这增加 Android 版本适配与原生维护成本，不作为默认建议。

## 环境和可复现输入

日期：2026-10-27；只读独占 `ImPadRuntimeSmoke` 实例 `emulator-5586`，Android API 37、arm64-v8a、16 KB 系统。未操作用户手机；不是三个真实应用的完整运行验收。

- 精确源码提交：`c9cd12a14b75409bebd2f73e4dfc5ff575df3eb8`，与 `gradle/dexkit-lock.json` 一致。
- jar SHA-256：`34a8c16d548aa7d8562128f4d2293cec4e0f9896bb1f46224dcd532e878900f5`。
- native SHA-256：`0444cf15146c256e8bbd2154ae8b120130dab47e9226c7e853a7d653737a95cd`，同时核对探针加载模块中的原生库。
- 报告：`work/effective-code-feasibility/avd-20261027-01/probe.json`，SHA-256 `a5a7f21aba34fc5bd17c71d735e26b8c8c62eced59de6fa8f110e1b97db7be3d`。
- 原始命令、耗时、stdout/stderr、导出 DEX、fixture 内容和 logcat 保留在同一目录；逐 fixture 源码和探针身份写入报告。
- Debug 模块只是提供已锁定的 Kotlin/DexKit 加载环境，未修改或安装正式候选包，也未把它当作新正式 release 验收。

复现（先启动本任务独占 AVD，使用新的输出目录）：

```bash
python3 scripts/probes/effective-code/run_probe.py --serial emulator-5586 --output work/effective-code-feasibility/NEW-RUN-ID
python3 scripts/ci/effective_code_probe_report.py --report work/effective-code-feasibility/NEW-RUN-ID/probe.json --fixtures scripts/probes/effective-code/fixtures.json
```

运行器拒绝默认/实体设备序列号和已有输出目录；无 `wipe-data`、安装应用或账号操作。

## 精确版本的 API 边界

实现引用均为上述源码提交中的位置，本地审阅副本在 `work/effective-code-feasibility/dexkit-source/`，不是先前不完整的研究副本。

| API | 实现事实 | 缺少的保证 |
| --- | --- | --- |
| `create(ClassLoader, useMemoryDexFile)` | `dexkit/src/main/cpp/native-bridge.cpp:179` 开始读取该加载器自身的 pathList/dexElements；内存模式从 mCookie 提取 DEX，失败/compact 时存在按文件路径回读分支 | 不自动组合父加载器的委派，不返回每项是否回读磁盘的外部证明，不判断已定义类来自哪一项 |
| `getClassData(Class)` | `DexKitBridge.kt:245` 将 Class 转 descriptor | 没有读取实际 Class 的 DEX 归属 |
| `getMethodData(Method)` | `DexKitBridge.kt:277` 将 Method 转 descriptor，再按类定义映射查询 | 接受反射对象不等于识别它的活动定义 |
| 重复类定义 | `Core/dexkit/dexkit.cpp:1429` 从单一映射取定义，`PutDeclaredClass` 写入映射 | 不表达完整 Java 委派及已驻留类语义，不能用合并查询确定胜者 |
| `exportDexFile` | `Core/dexkit/dexkit.cpp:510` 使用 `classes_<字节数>.dex` 并以 `wb` 写入 | 等长 DEX 会覆盖；导出文件数不一定等于输入 DEX 数，不能据此宣称输入完整 |

独立源码引用：[加载器桥](https://github.com/LuckyPray/DexKit/blob/c9cd12a14b75409bebd2f73e4dfc5ff575df3eb8/dexkit/src/main/cpp/native-bridge.cpp#L179)、[反射查询](https://github.com/LuckyPray/DexKit/blob/c9cd12a14b75409bebd2f73e4dfc5ff575df3eb8/dexkit/src/main/java/org/luckypray/dexkit/DexKitBridge.kt#L277)、[导出实现](https://github.com/LuckyPray/DexKit/blob/c9cd12a14b75409bebd2f73e4dfc5ff575df3eb8/Core/dexkit/dexkit.cpp#L510)。

## AVD 观测

| 用例 | 实际结果 | 按磁盘查询的字符串 | 按内存查询的字符串 | DEX/导出文件数（内存） |
| --- | --- | --- | --- | --- |
| single_dex | A | ['A'] | ['A'] | 1/1 |
| multi_dex | A | ['A'] | ['A'] | 2/2 |
| split_class | Extra | ['Extra'] | ['Extra'] | 1/1 |
| parent_first | A | ['B'] | ['B'] | 1/1 |
| delegate_last | B | ['B'] | ['B'] | 1/1 |
| whole_class_override | B | ['A'] | ['A'] | 2/2 |
| loaded_then_replace | A | ['B'] | ['A'] | 1/1 |
| loaded_then_prepend | A | ['A'] | ['A'] | 2/2 |
| export_completeness | A | ['C'] | ['C'] | 2/1 |
| unknown_loader | A | 未扫描 | 未扫描 | 未扫描 |

关键反例：

1. `loaded_then_replace`：Class 已执行 A，再用 rename 替换路径为 B。磁盘模式返回 B 的内容，内存模式返回 A；路径上的最新哈希不是已加载 Class 的身份。
2. `whole_class_override` 与 `loaded_then_prepend` 的当前输入同为 B、A，但实际 Class 分别执行 B、A。单凭当前 DEX 列表顺序不能重建是否已有 Class 驻留；本次 descriptor 查询两项均返回 A，前一项错误。不能依赖碰巧命中后一项就认证接口正确。
3. `parent_first`：真实运行 A，针对子加载器的桥查询 B；共享提供器必须自行明确建模父链。
4. `export_completeness`：A 与 C 的 DEX 不同哈希、长度同为 780 字节；桥报告两个 DEX，但只导出一个文件。本地固定样本直接证明丢失，不靠文件名推测。
5. `whole_class_override`：实际 B 不含 A 的 oldOnly 方法；探针验证反射不可见，防止今后把新旧类的方法拼接。

`unknown_loader` 在 native 扫描前明确拒绝，仅运行固定 oracle 验证其自定义委派。没有把它强行转成普通 path loader。

这些是样本边界内的实测结果；其他 Android 版本、ABI、compact/encrypted/in-memory 容器及应用自己的热更初始化均未验证。split_class 是模拟 base/child 加载结构，不冒充 PackageManager 真正安装 split 后的验收。

## 支持与性能矩阵

| 能力 | API 37 arm64 实测 | 其他版本 |
| --- | --- | --- |
| 普通/多 DEX 的内容读取 | 实验可读取；不代表来源证明成立 | 未测 |
| 此次 rename 替换后读取旧内存 DEX | `useMemoryDexFile=true` 获得 A | 未测；回读分支仍须防范 |
| 自动完整委派及已驻留定义归属 | 未满足 | 未证明 |
| 直接导出完整有序输入 | 等长覆盖反例失败 | 源码同一实现，运行未测 |
| 原设计严格运行绑定来源证明 | 不支持，不能以反射成功替代 | 未证明 |

测量方法：启动就绪后首次快照/解析、相同快照缓存命中、累计读取与索引次数、进程峰值 RSS/PSS 分开采集，固定同一设备镜像和同一 release 模块并保留原始值。本探针命令耗时已记录，但它不包含正式选择器、缓存或安装，不能作为产品性能基线。正式实现将采用用户确认的启动快照边界；产品基线与阈值仍须在对应运行入口接入前冻结。

## 阶段交付范围

本阶段新增探针、报告结构验证和实验文档；没有重构正式模块，没有更新 acceptance，也没有发布。报告校验通过仅证明结构及自洽性；并不把 `UNSUPPORTED` 变成运行支持，更不能替代补丁实际加载后的业务页面验收。

## 独立审查后的校验修正

审查确认原始十项 oracle 均与 fixture 一致，但发现结构验证器仅在 `SUPPORTED` 时比较预期。已补上与能力状态无关的 oracle 校验：任何结果的 expectedMarker 必须匹配固定样本；任何非空 observedMarker 也必须匹配。`UNSUPPORTED` 允许缺少运行观测，但不允许矛盾观测。新增反例先失败再通过，原始报告仍验证有效；实测能力结论不变。
