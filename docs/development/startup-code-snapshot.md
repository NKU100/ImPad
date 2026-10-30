# 启动代码快照与查询基础

## 当前状态

已实现共享基础，尚未切换正式模块入口、选择器、缓存或 CI 验收协议。当前 1.1.2 候选仍是此前方案，不能把本阶段结果当作已发布修复。

用户确认的保证为 `STARTUP_FILE_SNAPSHOT`：在应用原有启动就绪回调中观察标准文件加载器，捕获稳定内容，按显式委派模型选择类定义。不认证已驻留 Method 的 ART 来源，不支持任意运行中注入或改码认证。

## 组件与维护职责

- `compat-core/.../compat/code/CodeSnapshot.kt`：无路径的内容身份、父/子优先顺序、完整 class definitions 的归属索引。索引从构造时保存的定义生成，查找成本与同名定义数量相关，不在每次查询重新遍历加载器图。
- `compat-rules/.../StartupCodeSnapshot.kt`：文件复制及内容复核，APK/JAR 的 classesN.dex 数值排序、原始 DEX 支持、逐 DEX 私有查询归档和生命周期管理。关闭不删除原文件。
- `compat-rules/.../EffectiveCodeRuleSession.kt`：借用快照，为每个 DEX 独立打开查询桥；按整类归属过滤方法和类，指令读取检查预期定义位置。会话关闭只关闭桥，由调用方随后关闭快照。
- `app/.../runtime/StartupLoaderLayoutReader.kt`：标准 PathClassLoader/DexClassLoader/DelegateLastClassLoader 的有序文件输入与 Boot 边界；将实际声明类加载器与快照预计的加载器做同一性比较。它没有按应用包名或补丁文件名分流。

查询视图默认查询就绪加载器命名空间，现已增加 sourceOf、inContextOf、inNamespace 和快照上下文接口；父方法的引用在父命名空间中解析，不能把子覆盖类强行用于父定义。详见 [完整方案选择基础](complete-plan-selection.md)。

## 使用和生命周期

`StartupRuntimeSnapshot.capture(readyLoader, privateStorage)` 捕获前后核对布局，并提供 `inputsStillCurrent()`。此检查同时核对加载器对象/有序路径和源文件内容；不能仅凭文件时间或大小判定一致。

调用方在快照存活期间打开 `EffectiveCodeRuleSession`，完成识别和绑定；注册前后仍需调用一致性检查。捕获期间检测到变化会清理私有副本并失败。捕获后的任意时间没有自动监控保证，后续安装协调器仍需落实共享重试预算和批次启用。

首版拒绝未知自定义加载器、未命名/内存 DEX、不可读文件、损坏归档及损坏 DEX，不退回 base。真实应用若使用未支持的加载器，应单独验证布局/委派能力，不能仅凭“继承 PathClassLoader”加入白名单。

## 验证证据

2026-10-27，本阶段运行：

| 检查 | 结果 |
| --- | --- |
| compat-core 全部测试 | 30 通过 |
| app debug 单元测试 | 89 通过 |
| app release 单元测试 | 89 通过 |
| compat-rules 明确选择的可移植测试 | 52 通过 |
| 探针 Python 针对性测试 | 16 通过 |
| debug APK 构建 | 通过 |
| 独立 Android 原生查询探针 | 五项布局通过，未知加载器拒绝 |

macOS 没有运行锁定 Linux native 规则 fixture 的环境；这部分不计为通过。本阶段没有修改 Python 业务验证器，原报告验证器的 489 项测试结果保留在前阶段记录。

Android 使用只读独占 `ImPadRuntimeSmoke` 的 `emulator-5586`、API 37、arm64-v8a、16 KB 镜像；运行后已关闭，只操作本任务实例。报告保留于 `work/effective-code-feasibility/startup-snapshot-avd-20261027-02/`，包含精确探针/模块/依赖哈希、命令与原始输出、fixture 和导出内容。

五项检查实际调用新捕获器及 DexKit 查询视图，并与真实 Class 的执行标记比较；读取有效方法指令、核对声明类加载器，并确认 B 覆盖 A 时不复活 A 的旧方法。它们验证固定布局，不代表三应用业务验收或完整 Tinker 补丁重放。历史严格来源实验报告保留原样，不改写为支持。

## 下一阶段的接入要求

完整方案选择、识别器穷尽状态、微信编译形态收敛、快照身份缓存、绑定失败重查和安装回滚，以及 checker/报告/acceptance 的显式迁移须共同完成后再切换正式入口。新 core/code 与 runtime 提供器实现必须进入 resolver/cache 指纹，不能仅依赖整个源码归档的哈希。性能基线要在真实最终模块的首次解析、缓存命中和峰值内存上采集；当前小 fixture 不能替代大型应用基线。

独立审查发现并修复了一处指令读取绑定漏洞：读取前同时验证当前会话标记、原 descriptor、类名及 DEX 位置；不能通过复制并改写方法对象绕过整类覆盖，也不能使用其他会话留下的方法对象。两项回归测试先复现失败后通过，修复后的 debug APK 已重新通过上述五项 Android 查询探针。

归档仅支持从 classes.dex 开始的连续 classesN.dex 序列；有缺口时拒绝输入，避免把 ART 实际未打开的后续条目当成加载定义。该边界依据 [ART OpenAllDexFilesFromZip 实现](https://android.googlesource.com/platform/art/+/981d59093d03b1db7c19c03afb9c71ce5f1591c8/libdexfile/dex/dex_file_loader.cc)，它从 classes.dex 开始，按序打开，在缺失条目处停止。数值排序测试使用完整 1–10 序列并打乱 ZIP 插入顺序；缺少首项和中间缺口另有拒绝测试。
