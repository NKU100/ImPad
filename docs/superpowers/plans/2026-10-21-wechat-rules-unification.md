# 微信规则迁移与统一流水线实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将微信 8.0.69 / versionCode 3040 接入共享 RULES runtime，并让微信、QQ、企微只使用同一规则 checker 与流水线，完成同一最终 APK 的三应用验收。

**Architecture:** 微信新增不可变的 Kotlin 规则与策略，通过既有 Tinker-ready、身份校验、DexKit session、解析缓存和整组 Hook 安装接口运行。统一现有 checker、登记和 workflow 路径后，删除正式路径对 LEGACY、旧 targets 和旧 descriptor runtime 的依赖；三个应用使用固定 acceptance 代表回归及同一最终模块 APK 做独立 raw runtime 验收。

**Tech Stack:** Kotlin、Android/libxposed、Tinker、DexKit、Gradle、Python 3、GitHub Actions、x64 DevCloud（Linux 静态检查与构建）、Oracle ARM VPS（ARM64 runtime）。

**Spec:** [已批准设计](../specs/2026-10-21-wechat-rules-unification-design.md)。背景：[runtime feature matching 设计](../specs/2026-10-19-runtime-feature-matching-design.md)和[原 WeChatPad 设计](../specs/2026-10-04-wechatpad-design.md)。

## Global Constraints

- 微信首批基准固定为官方微信 8.0.69 / versionCode 3040，只覆盖 `tablet`、`login` 两个完整目标。
- 规则依据调用关系、方法形状和业务回调约束建立；不得把旧 JSON 中的混淆名称或 descriptor 直接复制为查询特征。
- 复用 `RuleSession`、共享 resolver、Tinker-ready bridge、身份与 Method/ClassLoader 检查、缓存及批量原子 Hook 安装。
- QQ、企微已有规则、策略、专用辅助行为及已有 acceptance 记录不可改写；增加新验收记录只能追加，原记录字段、首次代表和证据引用保持不变。
- 微信首次 acceptance 仅由主代理本地受控执行：固定 `appId=wechat`、8.0.69 / 3040；只有 `compatibility/wechat/acceptance.json` 真正不存在才可创建。任何已有文件（包括空文件或损坏文件）都中止，不覆盖、不修复性重建、不用缺表自动触发迁移。
- 首次登记复用 `read_acceptance_snapshot`、`verify_validation_files`、`make_record` 的显式迁移 API；先生成并审阅 `recordDraft`，再用 `O_CREAT|O_EXCL` 等价的独占创建写入，禁止 `os.replace`。普通 CLI 与 workflow 不暴露 initial-migration 开关；登记后日常 verify/register 均为非迁移模式。
- 历史 `compatibility/{wechat,qq,wecom}/targets.json` 可保留为历史资料；APK 不打包这些文件，正式 runtime、checker、回归、候选准备、适配和 workflow 均不得读取或消费它们。
- 最终正式执行路径没有 `RULES`/`LEGACY` 选择、旧 profile 回退或旧 descriptor resolver/cache/assets 生成分支；仅删除经引用审计证明只服务旧路径的代码与依赖。
- 最终 runtime 验收绑定同一份最终模块 APK 原始字节及 SHA；不同 APK 的旧 runtime 结果不能替代。每个应用均执行固定 acceptance 回归；常规窗口按既有规则最多三个代表 APK。
- 微信不登录、不扫码，不把进入二维码页表述为服务端登录成功；只验收模块开关下的平板入口、稳定二维码页、Tinker/native/Hook 时序及生产 raw verifier。
- 静态查询、测试与构建用用户 x64 DevCloud；ARM64 runtime 用 Oracle VPS 上独占任务容器。不得新增本机 VM、派发 GitHub Actions 来代替这些环境，亦不得使用当前环境外未经指定的设备。
- 当前 Godel 正在对企微 5.0.12 执行冻结 d080 验收。不得读取、重启、改写、清理或争用其设备、容器、ADB 端口、卷和冻结输入；Oracle runtime 阶段须等该任务释放资源后再开始，使用本任务独占容器和数据卷。
- 计划实施按连续任务推进，不增加人工阶段确认。每个实现任务本地完成独立 review gate 后继续下一任务；最终验收通过后可进行本计划授权的 GitHub 验证闭环。
- GitHub 操作前检查 `git status --short --branch`、`git branch -vv`、`git remote -v`、`git config user.name` 和 `git config user.email`，确认仓库用途为个人账号 `NKU100` / `21164383+NKU100@users.noreply.github.com`，确认目标是当前功能分支及其正确 GitHub remote，不在公共分支提交。
- 按依赖将现有未提交的 runtime-feature-matching 改动与本次改动整理为多个可独立审查的本地 commit；每次提交前检查 staged path、diff、secret 和体积。不得提交临时工作区、密钥、APK 下载缓存、设备状态、私密报告或未经筛选的数 GB raw runtime 数据。原始验收证据须持久保存在任务证据归档中；仓库只纳入必要的小型 manifest、哈希、verifier 输出摘要及可审查索引。
- 仅推送功能分支并运行本计划所需的 GitHub Actions 验证，按失败证据继续修复、提交、推送和复验；不合并主干、不发布、不打 tag。不要因排队、跳过或早期失败 job 宣称验证通过。
- 不提交、推送或创建 PR，直到最终三应用验收及本地审查通过；以上用户授权覆盖本计划内功能分支提交、推送与 Actions 验证，不覆盖合并、发布或打 tag。

## Review Focus

- 微信 tablet/login 特征在 8.0.69 上有且仅有一个真实定义；缺失、重复候选及语义相似干扰必须分别失败关闭。测试：`WeChatTabletLoginRuleTest` fixture 覆盖零、一、多命中和调用关系/回调语义干扰，并在 Linux checker 对官方 APK 两个目标各唯一匹配。
- 解析期间 APK/split 输入发生变化时不得安装旧计划；缓存不得跨 APK、签名、路由、策略、规则或 DexKit 身份复用。测试：`RuleInstallCoordinatorTest` 与 `RuleRuntimeResolverTest` 覆盖 stale input 重解析、身份和实现指纹变化失效。
- 首次登记目标缺失、空文件、损坏文件或并发创建时均保持不覆盖；普通 verify/register 不能进入迁移模式。测试：`test_acceptance.py`、`test_runtime_task.py` 对每种已存在目标拒绝、O_EXCL 冲突和普通 CLI fail-closed 断言。
- 微信保留 Tinker-ready 后再解析与安装，所有 Method 形状和 ClassLoader 归属验证后整组安装；失败需回滚且不留下部分 Hook。测试：`WeChatAppAdapterTest`、`WeChatHookInstallerTest` 覆盖 loader 时序、目标缺失、形状不符、回调行为及批量回滚。
- 三应用最终 runtime 证据必须绑定相同最终模块 SHA、各自精确官方 APK SHA 与本轮新鲜原始证据；既有 QQ/企微 acceptance records 不被重写。测试：`test_runtime_task.py`/`test_acceptance.py` 覆盖模块哈希绑定、每应用 raw verifier、历史 record 不变及最终清单同模块断言。

---

## 文件与接口地图

| 边界 | 文件职责 |
|---|---|
| 规则声明 | `compat-rules/src/main/kotlin/io/github/nku100/impad/rules/wechat/WeChatTabletLoginRule.kt`：实现 `RuleQuery.definition` 与 `resolve(session: RuleSession): RuleResolution`，声明两个必需 target、唯一 rule/strategy ID 和 3040 起始路由。 |
| 规则 catalog | `compat-rules/src/main/kotlin/io/github/nku100/impad/rules/RuleCatalog.kt`：把 `wechat` 映射至微信 catalog；其他应用映射不变。 |
| 微信 adapter / hooks | `app/src/main/kotlin/io/github/nku100/impad/apps/wechat/WeChatAppAdapter.kt`、`WeChatHookInstaller.kt`、`WeChatHookCallbacks.kt`：实现既有 `prepareClassLoader(loader, registrar, onReady)`、`installPlan(loader, plan, registrar, log, inputsStillCurrent): PlanInstallOutcome`；按显式 `strategyId` 绑定 tablet 与 login 行为。 |
| 规则测试 | `compat-rules/src/test/kotlin/io/github/nku100/impad/rules/wechat/WeChatTabletLoginRuleTest.kt` 与同目录 DEX fixture：证明特征、反例和唯一性。 |
| runtime 测试 | `app/src/test/kotlin/io/github/nku100/impad/apps/wechat/WeChatAppAdapterTest.kt`、现有 `WeChatHookInstallerTest.kt` / `WeChatHookCallbacksTest.kt`：证明时序、方法形状、策略、整组安装/回滚。 |
| 统一 checker | `compat-checker/.../Main.kt`、`RuleCheck.kt`、`CandidateReportWriter.kt`：正式静态入口使用 `check-rules --app-id <id> --apk <apk> [--abi-splits ...] --report <json>` 并返回共享 catalog 和 selection。 |
| Python 验证/登记 | `scripts/ci/acceptance.py` 的 snapshot、验证和 record API；`scripts/ci/runtime_task.py` 的 `verify_validation_files`、`verify_cli`、`register_cli`；`scripts/ci/static_regression.py` 的固定代表回归。普通入口保持 `initial_migration=False`。 |
| 候选与环境控制 | `scripts/ci/app_policy.py`、`candidate_selection.py`、`runtime_adaptation.py`、`redroid_prepare_apps.py`、`codex_adaptation.py`、`codex_task.py` 和 `scripts/ci/apps/*`：使用应用策略与共享 RULES 报告，移除旧 targets/profile 分支。 |
| Actions | `.github/workflows/build.yml`、`compatibility-app.yml`、`runtime-validation.yml` 与 `compatibility-{wechat,qq,wecom}.yml`：保留现有 workflow 和每应用入口，删除 LEGACY 条件并将三应用接入同一 checker、回归和 runtime。 |
| APK 元数据/旧 runtime | `app/build.gradle.kts`、`compat-core/build.gradle.kts`、`compat-rules/build.gradle.kts`、`settings.gradle.kts`、`compat-core/.../AppCompatibilityPolicy.kt`、`ImPadModule.kt`、`AppAdapter.kt` 及相关旧 resolver/cache/tests：删除生成/读取 targets 的路径、模式枚举和仅供旧 descriptor 流程使用的实现。 |
| 历史与验收资料 | 保留三个 `compatibility/<app>/targets.json` 原字节；不把它们加入 APK。追加式 acceptance 不改变 QQ/企微既有 record。证据索引放入 `evidence/wechat/` 与最终验收清单；大体积原始材料保存在任务证据归档，不进 Git。 |

## Task 1: 微信真实规则、Tinker runtime 接入与首次受控登记

**Files:**
- Create: `compat-rules/src/main/kotlin/io/github/nku100/impad/rules/wechat/WeChatTabletLoginRule.kt`
- Create: `compat-rules/src/test/kotlin/io/github/nku100/impad/rules/wechat/WeChatTabletLoginRuleTest.kt` 和测试用 fixture
- Modify: `compat-rules/src/main/kotlin/io/github/nku100/impad/rules/RuleCatalog.kt`
- Modify: `app/src/main/kotlin/io/github/nku100/impad/apps/wechat/WeChatAppAdapter.kt`
- Modify: `app/src/main/kotlin/io/github/nku100/impad/apps/wechat/WeChatHookInstaller.kt`、`WeChatHookCallbacks.kt`
- Create/modify: `app/src/test/kotlin/io/github/nku100/impad/apps/wechat/WeChatAppAdapterTest.kt`、`WeChatHookInstallerTest.kt`、`WeChatHookCallbacksTest.kt`
- Modify: `compat-core/src/main/kotlin/io/github/nku100/impad/compat/apps/wechat/WeChatCompatibilityPolicy.kt` 与微信 runtime policy `scripts/ci/apps/wechat/runtime_policy.py`
- Evidence: `evidence/wechat/8.0.69-3040/` 中保留 APK/module/source identity、小型 manifest、checker 报告和生产 verifier 输出；大型 raw dump 放任务证据归档。
- Controlled local acceptance: `compatibility/wechat/acceptance.json` 仅在目标真正不存在时创建。

**Interfaces:**
- Consumes: `RuleQuery.resolve(session: RuleSession): RuleResolution`、`RuleSession.findMethods(MethodQuery)`、`SharedRuleResolver.resolveDetailed(identity, catalog, session)`、`RuleRuntimeResolver.resolve(build)`。
- Produces: `RuleCatalog.forApp("wechat")` 返回 appId `wechat` 的 catalog；新增不可变 rule ID `wechat_tablet_login_001`、strategy ID `wechat_tablet_login_behavior_001`、完整 target 集合 `{tablet, login}`，版本 binding 从 3040 开始并开放上界。规则的 matcher 细节由真实 APK 的 DEX/调用链分析确定并以 fixture 固化，不从 targets descriptor 抄取。
- Adapter contract: `prepareClassLoader(loader, registrar, onReady)` 使用微信现有 Tinker bridge；`installPlan(loader, plan, registrar, log, inputsStillCurrent)` 先解析并核对两个实际 Method 的 class、参数、返回值、static 属性和 ClassLoader，再交给既有批量 Hook installer。
- First-record API: 主代理仅本地调用现有 `read_acceptance_snapshot(path, "wechat", True, ...)`、`verify_validation_files(... initial_migration=True ...)` 和 `make_record(... initial_migration=True ...)`。新增私有、不可从 argparse/workflow 调用的两段操作：`prepare_initial_wechat_record_draft(validation_inputs) -> dict` 和 `create_initial_wechat_acceptance(draft, acceptance_path) -> dict`；前者固定 appId、versionCode、目标路径且输出待审 draft，后者复核目标不存在后以独占 create 写入，随后用普通 loader 验证。不得以 CLI flag 开启。

- [ ] **Step 1: 分析官方微信 8.0.69 / 3040 的真实 tablet 与 login 逻辑**。固定 APK 原始 SHA，检查 DEX 调用关系、方法形状及 Hook 回调约束；记录可公开审查的特征依据和负样本，不把旧 JSON 名称当作特征。
- [ ] **Step 2: 建立 Kotlin fixture 与规则测试**。测试 target 集合精确为 `tablet`、`login`；各自验证缺失、唯一、多个定义、形状伪装但调用语义错误，以及两目标不完整时整体不返回 `Unique`。
- [ ] **Step 3: 实现不可变规则与 catalog 路由**。在 Linux x64 DevCloud 执行 `./gradlew --no-daemon :compat-rules:test :compat-core:test :compat-checker:test`；新旧 QQ/企微规则 fixture 均通过，微信 fixtures 与 8.0.69 对 tablet/login 各返回一个定义。
- [ ] **Step 4: 接入微信 Tinker runtime**。实现 adapter 的 ready bridge 与 `installPlan`，策略 ID 明确绑定原有微信 tablet/login 回调；解析或任一 Method 检查失败时不注册 Hook，注册部分失败时批量回滚。运行 `./gradlew --no-daemon :app:testDebugUnitTest`。
- [ ] **Step 5: 对官方基准执行 Linux 真实 checker**。使用 `./gradlew --no-daemon :compat-checker:run --args='check-rules --app-id wechat --apk <official-8.0.69-3040.apk> --report <report.json>'`；核验 package、versionCode=3040、ABI、签名、APK SHA、catalog/runtime identity 和 tablet/login selection 都与任务输入相符。任何不唯一或身份不符都停止。
- [ ] **Step 6: 在 Oracle ARM VPS 独占容器验收微信行为**。待 Godel 的企微 5.0.12 冻结 d080 验收释放资源后再启动；安装本任务精确微信 APK 与模块原字节，模块关闭时无平板入口、开启时入口出现并稳定进入 `LoginAsExDeviceUI`；记录 Tinker-ready、DexKit API/native、Hook 时序、APK/module/source 哈希，运行 production raw verifier。不得登录或扫码。
- [ ] **Step 7: 首次微信 acceptance 生成 draft 并独占创建**。同一已校验 snapshot 和 raw verifier 结果经过 `verify_validation_files`，空历史明确输出 `NO_HISTORICAL_SAMPLES`；调用 `make_record` 生成 draft，确认 source/catalog/plan/persisted evidence 与精确 APK/module 绑定。目标文件若在任意时点存在即停止；主代理审阅后用 `O_CREAT|O_EXCL` 等价操作创建，再用普通 loader 校验新表。
- [ ] **Step 8: 覆盖首次登记 fail-closed 测试**。扩展 `scripts/test_acceptance.py` 与 `scripts/test_runtime_task.py`：缺失文件可准备 draft；已有非空、空、损坏文件均不能覆盖；create 竞争得到 `EEXIST` 且原字节不变；`verify`/`register` CLI 与 workflow 无 migration 参数且缺失/空/损坏表失败。运行 `python3 -m unittest discover -s scripts -p 'test_acceptance.py'` 和 `python3 -m unittest discover -s scripts -p 'test_runtime_task.py'`。
- [ ] **Task 1 review gate:** 独立审查仅覆盖微信规则、adapter、首次登记边界和证据绑定；确认 QQ/企微规则、策略与 acceptance record 未改。该 gate 通过后进入 Task 2。

## Task 2: 统一 checker/workflow 并删除全部正式 LEGACY 路径

**Files:**
- Modify: `compat-checker/src/main/kotlin/io/github/nku100/impad/checker/Main.kt`、`RuleCheck.kt`、`ApkIdentityInspector.kt`、相关 checker tests
- Modify: `compat-core` 的 app policy、build logic、旧 resolver/model 与 tests；`compat-rules/build.gradle.kts`、`compat-core/build.gradle.kts`
- Modify: `app/src/main/kotlin/io/github/nku100/impad/ImPadModule.kt`、`runtime/AppAdapter.kt`、registry及旧 descriptor resolver/cache；`app/build.gradle.kts` 与 metadata tests
- Modify: `scripts/ci/app_policy.py`、`candidate_selection.py`、`static_regression.py`、`acceptance.py`、`runtime_task.py`、`runtime_adaptation.py`、`redroid_prepare_apps.py`、`codex_adaptation.py`、`codex_task.py`
- Modify: `.github/workflows/build.yml`、`.github/workflows/compatibility-app.yml`、`.github/workflows/runtime-validation.yml`；检查 `.github/workflows/compatibility-{wechat,qq,wecom}.yml` 并仅在入口参数仍携带旧 profile/mode 时修改
- Modify: `.github/codex/adapt-wechat.md`、`adapt-qq.md`、`adapt-wecom.md` 与 `docs/development.md`、`docs/validation/compatibility-check-ci.md` 中与旧路径有关的操作说明
- Delete: `compatibility/resolution-modes.json`、`app/src/main/kotlin/io/github/nku100/impad/runtime/RuntimeCompatibilityResolver.kt`、`RuntimeResolutionCache.kt`、`compat-core/src/main/kotlin/io/github/nku100/impad/compat/CompatibilityTarget.kt`、`CompatibilityResult.kt`、`CompatibilityResolver.kt`、`CandidatePipeline.kt`、`DexFactReader.kt`、`DexMethodFact.kt`、`HookMethodMatcher.kt`、`ResolutionCache.kt` 及其仅 legacy 测试/依赖（逐项以调用图确认）；删除旧 targets 资产生成器。保留 `compatibility/{wechat,qq,wecom}/targets.json` 历史文件原样。
- Tests: `compat-core/src/test/.../ResolutionModeConfigurationTest.kt` 等旧模式测试移除或改为单一路径断言；扩展 `scripts/test_app_policy.py`、`test_candidate_selection.py`、`test_static_regression.py`、`test_runtime_validation_workflow.py`、`test_build_module.py`、`app` metadata tests。

**Interfaces:**
- Consumes: Task 1 的 `RuleCatalog.forApp(appId)`、`RuleCheck.check(appId, apkFiles)`、`check-rules` 报告、三个应用策略和经过受控登记的 acceptance。
- Produces: 对每个 `appId` 都固定经共享 checker 的候选、固定 acceptance 回归、runtime 验证和 report 结构；应用策略保留身份、ABI、进程、规则/策略、来源 URL 与行为验收差异，但不含 resolution mode 或 targets 路径。
- 保留命令职责：`check-rules` 为静态唯一入口；`static_regression.run_regression(app_id, reports, ...)` 使用 acceptance 固定代表；`runtime_task.verify_validation_files(...)` 为 raw file-backed runtime verifier；普通 `verify_cli`/`register_cli` 永远传 `initial_migration=False`。

- [ ] **Step 1: 收敛静态 checker 与策略 schema**。移除 `check --targets`、旧 candidate/profile analyzer 和 `targetsPath` 消费端；明确 appId，不再有隐式微信默认。所有应用走 `RuleCheck` / `SharedRuleResolver`，checker 报告字段一致。
- [ ] **Step 2: 收敛 Android runtime 与 APK 元数据**。从 `ImPadModule` 删除 `initializeLegacy`、`readTargets`、`hasTargetAsset` 分支；`AppAdapter` 删除 `resolutionMode`、旧 descriptor `install` 与 targets asset contract；删除 `RuntimeCompatibilityResolver`、`RuntimeResolutionCache`、旧 profile model，仅在调用图确认无新路径使用后删除其依赖。metadata 检查断言 debug/release APK 不含任何 `assets/compatibility/**/targets.json` 或 `assets/compatibility-local/**`。
- [ ] **Step 3: 统一候选、回归、登记与适配控制器**。移除 `app_policy.py` 对 `resolution-modes.json` 的加载和所有正式 `resolution_mode == LEGACY/RULES` 分支；候选选择、runtime task、静态回归和 Codex 控制器都使用 acceptance/catalog/report。保留与 profile 无关的旧 Issue/checkpoint 历史兼容逻辑，除非引用审计证明它是旧 targets 的专用消费者。
- [ ] **Step 4: 合并现有 Actions 流程**。在 `build.yml`、`compatibility-app.yml`、`runtime-validation.yml` 和三个 `compatibility-*.yml` 入口里删去 profile 下载、exact legacy replay、LEGACY-only jobs/conditions/outputs；静态 build 对三个 app 运行统一固定代表回归，runtime/adaptation 的输入和结果都使用相同 candidate report schema。不新建平行 workflow。
- [ ] **Step 5: 清理资产生成和孤儿依赖**。删除 app Gradle `GenerateCompatibilityTargets` 及 resolution-modes 输入、兼容性目标打包检查中的旧模式扫描；将 metadata 检查改为三个应用皆无 targets assets。运行 Gradle dependency insight 与 `rg` 调用图，删除只被旧 descriptor、旧 checker 或旧 targets generator 引用的库及许可证条目；共享身份校验、DexKit、下载、恢复、安全隔离、UI/raw verifier 工具继续保留。
- [ ] **Step 6: 替换旧测试与文档断言**。移除测试中要求 LEGACY 行为的 case，新增全应用规则 checker、登记 fail-closed、targets 不读取/不打包、旧 mode 文件不存在且无正式代码引用的静态审计断言。保留 QQ/企微现有规则/策略 fixture 和验收 record 精确不变。
- [ ] **Step 7: 执行 x64 DevCloud 静态验证与构建**。运行 `./gradlew --no-daemon :compat-core:test :compat-rules:test :compat-checker:test :app:testDebugUnitTest :app:verifyModuleMetadata :app:assembleDebug`、`python3 -m unittest discover -s scripts -p 'test_*.py'`、workflow YAML lint 和 `git diff --check`。将同一源码指纹、DexKit lock/native identity、测试 XML、构建日志与 APK SHA 归档；真实 APK checker 回归含微信 8.0.69 和固定 QQ/企微 representatives。
- [ ] **Step 8: 完成 legacy 残余引用审计**。对正式运行、checker、候选准备、回归、prompt、登记与 workflow 路径执行 `rg -n 'LEGACY|resolution-modes|targets\.json|compatibility-local|expectedDescriptor'` 并逐项分类；历史 JSON、历史设计/报告中允许留存，正式代码和 workflow 不得存在 profile/targets 消费者、mode 开关或旧 descriptor fallback。将清单和结论写入 `scratch/wechat-unification-plan-report.md` 的实施结果部分/后续验收记录。
- [ ] **Task 2 review gate:** 独立审查 checker、runtime、scripts、workflow 和 Gradle 删除范围；要求三应用统一入口可追踪，旧路径无消费者，QQ/企微现有规则、策略和记录未被改写。该 gate 通过后进入最终 APK 验收。

## Task 3: 最终构建、固定回归、三应用 raw runtime 验收与 GitHub 验证

**Files / Evidence:**
- Modify/add only as needed: 三应用 `compatibility/{wechat,qq,wecom}/acceptance.json`（微信在 Task 1 建立首条；最终模块可追加新验证记录；既有 QQ/企微 record 逐项不变）、`evidence/wechat/`、`evidence/qq/`、`evidence/wecom/` 的精简 manifest 和 verifier 输出索引、`docs/support.md` / `docs/development.md` 中最终支持状态说明。
- Persistent evidence: 一个最终清单绑定源码指纹、catalog 和三份官方 APK 身份、同一最终 module APK SHA、各 app runtime bundle SHA、production verifier 状态与 raw archive locator。大型 screenshots/logcat/UI dump/raw probe 保存在任务证据归档，Git 只存必要的小型审计证据。

**Interfaces:**
- Consumes: Task 1/2 产出的最终单一路径模块、三应用规则 catalog、现有 QQ/企微 acceptance 与微信首条记录。
- Produces: `FINAL_ACCEPTANCE=VERIFIED` 清单，三 app 各有同一 module SHA 下新鲜 runtime 证据；固定代表 regression 为 `PASSED`（微信首次空历史仅允许在首次登记报告中标注 `NO_HISTORICAL_SAMPLES`，最终回归使用新增微信 runtime-verified record）。
- GitHub flow: 已授权的当前功能分支 reviewable commits → personal GitHub remote push → 对应 Actions run → 按真实失败继续修复与复验；不得 merge/release/tag。

- [ ] **Step 1: 固定最终源码、构建输入和唯一 APK**。x64 DevCloud 从当前任务功能分支源码构建 debug APK，记录完整 source fingerprint、依赖/native lock 和 APK SHA；后续三个 runtime job 只能安装该 APK 原字节副本，主源或模块字节变化即三 app runtime 结论全部重跑。
- [ ] **Step 2: 运行最终固定 acceptance 静态回归**。以 `static_regression.py --all-apps` 跑微信新增代表及 QQ/企微既有最多三个 representatives；核对每条 record 的 rule、strategy、targets，QQ/企微历史 record IDs/内容/firstRecordId 均保持，不能退化为读取旧 targets。
- [ ] **Step 3: 在 Oracle 完成微信最终 raw runtime**。Godel 企微冻结 d080 任务已释放后，使用本任务新建独占容器/volume 和独立 ADB 端口；运行模块关闭/开启场景、`LoginAsExDeviceUI` 稳定页面、Tinker/native/Hook 时序和 production raw verifier，证据绑定 Task 3 的唯一模块 SHA 与官方 8.0.69 APK SHA。
- [ ] **Step 4: 在同一 Oracle 最终模块上完成 QQ 与企微 raw runtime**。QQ 9.3.1 / 14378 复验自然请求 phone/tablet AppID、socket/进程归属与 QFix/native/Hook 时序；企微使用固定验收基准和既有断言完成模块开关入口、二维码页、Tinker/native 和所有 Hook 的 raw verifier。每个 bundle 中的 module APK SHA 必须与 Task 3 Step 1 一致；不能引用 d080 或其他旧 runtime 结果作为通过证据。
- [ ] **Step 5: 通过正式生产 verifier 并形成验收清单**。对三个 app 的原始证据分别执行现有 `runtime_task.py verify` file-backed 验证，确认 `outcome=VERIFIED`；报告 APK/module/source/catalog/plan/acceptance snapshot hashes、入口/请求断言与 verifier SHA。任一应用缺 raw evidence、输入 SHA 不同、仅有截图或仅有 module-installed 信号都不通过。
- [ ] **Step 6: 完成最终本地 review 与依赖提交整理**。主代理审阅 Task 1–3 diff 和证据 manifest；按依赖把已有 runtime-feature-matching 改动与本次变更分为多个 reviewable commits。逐个 `git add` 精确文件组；因仓库 `.gitignore` 当前忽略 `docs/superpowers/plans/`，计划文件只用精确路径 `git add -f docs/superpowers/plans/2026-10-21-wechat-rules-unification.md` 纳入。提交前审查 `git diff --cached --name-status`、`--stat`、`--check`、大文件清单、密钥扫描和 QQ/企微 acceptance 对比。提交语言沿用 `git log`，不添加 AI co-author。
- [ ] **Step 7: GitHub 身份/目标复核后推送功能分支**。先检查 branch/upstream/remotes 和 Git author；确认个人 `NKU100` 身份、remote 属于个人 GitHub 仓库、目标 ref 为 `feature/runtime-feature-matching` 且不是 main/master。若当前 branch 跟踪公共分支先解除错误 upstream；按精确 refspec 首次 `git push -u <verified-github-remote> feature/runtime-feature-matching`，不 force push。
- [ ] **Step 8: 运行 GitHub 验证并修复循环**。先收集推送触发的 `build.yml` required checks；如还需验证候选 workflow，只 dispatch `operation=verify` 且 `run_codex=false` 的验证路径，不启动 adapt/model/PR 或 acceptance 写入。不得触碰 Godel 的冻结任务或向其环境派发作业。失败时读取完整 job/log/artifacts，修复后回到 Task 2/3 受影响步骤、重新生成受影响 APK/runtime evidence、按依赖提交并推送，直至相关 required jobs 成功。跳过、取消、排队、启动失败或只跑轻量 smoke 均不能算最终通过。
- [ ] **Step 9: 收口最终记录但不合并**。确认远端功能分支包含所需 commits、GitHub 验证已完成、三 app manifest 与验收记录都绑定最终模块，原始大材料位于持久任务归档且仓库没有临时/私密大文件；更新短报告，明确 Actions run、commit 和验证状态。停止在可审查功能分支，等待早间主线程查看结果；不 merge、不发布、不打 tag。
- [ ] **Task 3 review gate:** 独立审查最终提交差异、证据完整性与 GitHub 结果；拒绝任何跨 APK runtime 证据复用、历史 record 改写、未运行 job 的绿灯推断或原始大体积材料误提交。

## 实施调度与完成条件

Luna 连续执行 Task 1 → Task 2 → Task 3；主代理协调环境与独立审查，不亲自实施。任务间不等待额外人工确认。Task 1/2 的每个独立 review gate 通过后继续后续任务；如果 Godel 冻结验收仍占用 Oracle，Luna 先完成可并行的规则、checker、脚本、测试和 x64 构建，再停止新增 Oracle runtime 作业，待资源释放后继续。

计划完成必须同时满足：微信 8.0.69 两目标真实静态唯一、真实 Tinker runtime/raw verifier 通过；首次 acceptance 由受控独占创建并普通 loader 验证；正式代码和 workflow 无 LEGACY/profile/targets 消费及 fallback；所有受影响测试、metadata、构建、workflow 校验及残余审计通过；最终同一 module APK 下微信/QQ/企微三份 raw runtime verifier 均 VERIFIED 且固定回归通过；GitHub 功能分支 required checks 成功；提交树不含密钥、临时材料或未经筛选的大型 raw 证据。不得把本计划的完成等同于合并、发布或主干交付。
