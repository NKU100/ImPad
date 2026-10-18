# 兼容检测 CI

当前发布模块包名为 `io.github.nku100.impad`，显示名为 `I'm Pad`。包名从旧版 WeChatPad 变更后，需卸载旧包再安装新包，并在 LSPosed 中为目标应用启用模块作用域。微信、QQ 与企业微信使用独立应用策略和 Target 清单；只有完成对应运行时验收并合入主分支的 profile 才代表正式支持。

`compatibility-app.yml` 是共享入口，仅由微信、企微和 QQ 各自的定时／手动 workflow 调用；默认从对应官方来源发现最新 ARM64 APK。下载后以 APK 内实际版本、官方签名和 SHA-256 为准。已正式支持且 APK 摘要未变化时提前结束，不启动静态矩阵、构建、Codex 或 ReDroid；手动选中 `force_recheck` 可重新静态检查。需要分析时再按候选版本选择静态回归窗口：新候选加上版本码最高的两个已正式支持旧版，已支持候选检查候选本身和最多两个较早的正式支持版本。报告列明本次实际检查的版本，历史 profile 保留。

## 检查流程

1. 已登记 APK 只允许使用清单中的 HTTPS 腾讯 CDN 地址 `dldir1v6.qq.com/weixin/android/`，并以预期 APK SHA-256 为缓存键。
2. 最新候选从 `https://weixin.qq.com/` 首页发现。脚本只接受官网列出的腾讯 CDN ARM64 直链，并选择唯一的最高版本；没有候选、多个并列候选或页面结构变化都会失败，不切换到第三方来源。
3. 候选版本码用于选择最多两个较早的 `runtime-verified-local` 或 `runtime-verified-hosted` profile；候选自身另行检查，因此每轮最多覆盖三个不同版本。
4. 候选 APK 以官方直链 SHA-256 定位缓存，文件按实际 SHA-256 命名。缓存命中时会核对官网的 `Last-Modified`、`Content-Length`、APK 摘要和已登记摘要；信息缺失或变化时重新下载。
5. `compat-checker` 调用 Android 模块共用的 `compat-core`。正式 profile 解析与未知候选分析共用 `HookMethodMatcher` 对锚点、参数/返回形状和唯一性的判断；未知版本使用最近的较早正式 profile，正式版本还要求方法描述符与登记值完全一致。
6. 未知版本只有在基线中所有 Hook 都标记 `safeForForwardInference: true` 且每个 Hook 唯一匹配时，才会生成候选 profile 建议。未标记、缺失、形状变化或多重命中都会进入 `NEEDS_HOOK_REVIEW`，不会猜测 Hook 目标。

## 检查结果

每次成功发现候选都会生成 `candidate-report.json`，包括候选包名、版本、ABI、签名、APK SHA-256、来源、逐 Hook 结果、基线版本、回归版本、流水线状态、阻塞原因，以及安全时生成的候选 profile。报告作为 14 天 artifact 上传，状态和 Hook 诊断也会写入 Actions job summary；最终状态通过 `latest` job output `pipeline_status` 暴露给后续步骤。

候选经过静态检查和回归后，最多到 `STATIC_VERIFIED_PENDING_RUNTIME`。状态不会由 CI 改写为正式支持；候选必须通过独立托管 ReDroid 验收，再由开发者审阅、合并 profile。未登记构建在主分支上继续进入 Codex 适配任务；已登记构建只检查现有 profile。Codex 成功的改动进入 draft PR，候选 profile 保持 `static-verified`。

候选发现或下载失败会生成 `FETCH_FAILED` 报告；profile 清单或回归窗口无效会生成 `BASELINE_INVALID`；已登记回归失败会生成 `STATIC_REGRESSION_FAILED`。未知候选需要人工分析时，`NEEDS_HOOK_REVIEW` 是可供流水线继续分支处理的结果，不会被误报为 `UNKNOWN_BUILD`。

在仓库的 **Actions** 页面选择 **Check WeChat compatibility**，再点 **Run workflow** 即可运行。静态检测不使用签名 Secrets；新候选的后续托管冒烟会安装 Magisk/LSPosed 并启动 ReDroid。

## 普通构建验证

`Build module APK` 在 push、PR 和手动构建中运行核心、检查器、应用及 Python 流水线单元测试，检查模块元数据与打包内容，并对版本码最高的最多三个正式支持构建做真实 APK 静态回归。每次代码构建都会重新检查，已支持状态不会跳过回归。APK 仅从已登记的官方 URL 获取，按 SHA-256 缓存，每次使用前重新校验摘要；检查失败阻止构建通过并上传每个构建的日志和结果报告。

普通构建和适配控制器共用 `scripts/ci/static_regression.py` 的下载校验及检查器调用。 普通构建显式使用 `--all-apps`，按微信、QQ、企微分别检查最近三个已运行验证的版本码，并保留同版本码下的所有 APK 变体。各应用使用独立缓存和报告目录，汇总结果列出实际 APK 身份；任一应用失败后仍检查其余应用，最终构建保持失败。适配仍覆盖候选加最多两个旧版，使用预下载 APK 离线检查。普通构建不调用 Codex 或 ReDroid。单元测试验证解析、拒绝、缓存和 Hook 注册等逻辑；真实 APK 静态回归验证目标解析；实际注入和界面行为由动态冒烟验证。

## 单版本运行验证

`Validate ImPad runtime` 用于重放一个已支持版本的构建和托管 ReDroid 验证。它选择官网最新 APK；若该精确 APK 尚未登记并完成运行验证，则停止并提示先运行兼容适配，不自动换成已支持的其他版本。

`Run ImPad ReDroid smoke` 的独立手动入口也检查官网最新 APK，使用同一套候选选择、官方下载和摘要校验逻辑。由适配流程调用时直接使用已经静态验证的精确候选产物。每次运行只启动一个版本的动态冒烟，静态回归仍最多覆盖三个版本。运行验证入口不调用 Codex、不创建 PR、不更新正式支持状态。

## Codex 适配

`run_codex` 默认开启。主分支手动或定时运行先检查本应用打开的适配 Issue，优先处理仓库所有者未消费的评论；没有新评论时检查官方最新 APK。只认可完整候选标记和机器人作者的记录，排除 PR。评论续接锁定原候选完整 APK 身份；新版发现复用同候选尚未完成的记录，手动关闭的记录不会重开。每轮保存代码补丁、候选报告、原生线程密文及诊断到不可变 Artifact，并更新 Issue 的恢复指针。Artifact 保留 90 天；Actions Cache 缓存 APK 和构建依赖，不作为唯一适配状态存储。恢复后重新校验官方 APK 的 SHA-256，复用 PR 前校验作者、候选和已记录提交。

每次运行只驱动一个 Codex app-server continuation turn，固定使用 `gpt-6-luna` 和 `xhigh`，关闭多代理能力，不设置累计 Token 预算，也不使用手动重试开关。没有累计失败次数或固定运行轮数的停止条件；模型未完成、静态验收失败或托管运行拒绝时，后续评论续接或再次发现同一候选时继续原 Issue。任务仍受 GitHub Actions 单次运行时限、订阅用量限制和平台故障影响。静态验证通过但还未完成运行验收时会复用已验证改动并重跑托管 ReDroid；只有精确候选通过独立运行时验收并达到 `RUNTIME_VERIFIED` 后才关闭 Issue。模型自述完成不能绕过独立验收。

托管 runner 安装发行版 bubblewrap，并加载可用的专用 AppArmor profile。调用模型前通过同一 app-server 执行 workspace-write 沙箱命令，检查候选文件可读和工作目录可写；检查失败时不创建 Goal、不消耗模型 Token。模型获得包含完整 Git 历史和全部已跟踪文件的独立 checkout、原始 APK 和完整候选分析报告，自行选择所需上下文，并根据候选 APK 和运行行为确认适配结论。独立 checkout 不配置远端，不复制原仓库工作目录中的未跟踪文件或 Git 配置。外层 bubblewrap 仅挂载独立 checkout、独立用户目录、认证目录和工具；Gradle 只复制依赖与 wrapper 缓存，不暴露原仓库、项目构建缓存和控制器状态。预检验证原仓库与控制器状态不可读。模型可使用 jadx 检查真实 APK。控制器拒绝修改既有 profile、检查器、脚本、工作流和构建配置。新增 profile 必须使用已校验的身份和来源，状态只能为 `static-verified`。候选和最多两个较早正式支持版本分别经过共用检查器；核心测试、模块测试和 debug APK 构建通过后才发布 draft PR，并把 Issue 状态更新为 `WAITING_RUNTIME`。模型失败或静态检查失败时，Issue 和结果报告停留在 `NEEDS_HOOK_REVIEW`。

PR 分支包含静态适配。流水线随后自动执行托管 ReDroid 登录界面冒烟，通过后记录结果并将候选更新为 `runtime-verified-hosted`；审阅合入主分支后再由兼容检测确认 `FORMALLY_SUPPORTED`。历史 AVD 记录只用于诊断参考。

## 订阅认证配置

此流程使用 ChatGPT 管理的 Codex 登录，不使用 OpenAI API Key；它消耗订阅中的 Codex 用量。仅在主分支手动或定时触发的可信工作流中使用；PR 或 fork 不可触发认证恢复。需要两个 Actions Secrets：

| Secret | 用途 |
|---|---|
| `CODEX_AUTH_JSON` | CI 独立登录生成的 managed ChatGPT 凭据 |
| `CODEX_AUTH_WRITE_TOKEN` | 将 Codex 自动刷新后的凭据写回 Secret |

为写回创建 fine-grained GitHub token，只选择此仓库，授予仓库 **Secrets: Read and write** 权限；无需给这个 token 代码写入或 PR 权限。将其录入 Secret：

```sh
gh secret set CODEX_AUTH_WRITE_TOKEN --repo NKU100/ImPad
```

然后执行独立登录脚本，在浏览器中完成 device-code 登录。脚本使用临时 `CODEX_HOME`，上传后删除本地临时凭据，避免和桌面会话共用刷新状态：

```sh
bash scripts/setup_codex_ci_auth.sh NKU100/ImPad
```

runner 在模型调用前恢复凭据，Goal 结束后即使模型失败也写回刷新后的文件，删除认证文件，再执行发布前的最终独立验收。写回 token 不传给模型进程。登录凭据和会话目录不上传 artifact。产物中的 `goal-diagnostics.json` 保留每轮验收结果、Goal 用量、模型最终说明及失败工具摘要；已知认证凭据和常见 Token 格式在写入前过滤。凭据失效或 token 到期需要重新配置；不得通过自动重复适配来尝试修复登录。

### 加密会话记录

仓库 Actions variable `CODEX_TRACE_RECIPIENT` 配置 age X25519 公钥（`age1…`），私钥只保存在本机，不放入仓库或 Actions。CI 安装 age，在调用模型前验证公钥。缺失、无效的公钥或加密工具故障会阻止模型启动。

控制器完整记录发给 app-server 的请求和收到的响应、通知，包括任务提示、工具调用及成功和失败命令的输出；每条 JSONL 记录包含 UTC 时间、方向和协议消息。记录不截断、不脱敏，直接通过管道送入 age 加密，不生成明文会话文件，也不写入 Actions 控制台或摘要。这里的完整记录指 app-server 对外提供的协议事件，不包含未公开的模型内部内容、认证文件、CLI 会话目录或原始 stderr。

模型成功或失败后，完成加密的 `session.jsonl.age` 和现有诊断文件一起作为 `impad-codex-adaptation` artifact 保存 14 天。加密失败会删除未完成密文；上传清单仅包含完成后的文件。runner 被强制终止时可能没有可恢复的完整记录。任何取得 artifact 的人只能获得密文，runner 在处理事件时仍可接触明文；仅使用可信主分支工作流。

本机安装 age 后，在仓库外用私钥解密，再交给本机 Codex 分析：

```sh
umask 077
age --decrypt --identity ~/.config/impad/ci-trace.agekey \
  --output session.jsonl session.jsonl.age
```

私钥丢失后无法解密旧记录，应在可信的本地存储中备份。更换公钥只影响后续运行，旧 artifact 仍需原私钥。生成新密钥可用 `age-keygen --output <私钥文件>`，公钥可用 `age-keygen -y <私钥文件>` 提取。

还需在仓库 Actions 设置中开启 **Allow GitHub Actions to create and approve pull requests**。GitHub 将创建与审批放在同一个开关；此流水线只创建 draft PR，不提交审批或合并。draft PR 发布使用权限受限的 `GITHUB_TOKEN`；由该 token 创建的 PR 通常不会自动触发其他 workflow，因此发布后显式手动触发构建 workflow。

参考：[OpenAI managed-auth CI 指南](https://learn.chatgpt.com/docs/auth/ci-cd-auth)、[GitHub Secret 写入权限](https://docs.github.com/en/rest/actions/secrets#create-or-update-a-repository-secret)。

## 托管运行时验证

兼容检测由主分支手动或定时触发。Codex 适配的静态检查和构建成功后，同一次运行调用 `runtime-validation.yml`，复用已校验的微信 APK 和模块 APK。托管 ReDroid 使用固定 ARM64 Android 14 容器、Magisk、Zygisk Next 和官方 LSPosed；工作流安装框架、启用模块并验证无模块基线。

通过标准为模块注入成功、Hook 安装无错误、Phone & Tablet 登录入口出现，且可进入稳定的二维码页。无需扫码、登录账号或验证双设备服务器会话。

成功输出 `RUNTIME_VERIFIED`，更新候选为 `runtime-verified-hosted`，记录 PR 和 Issue；合并后才正式支持。失败输出 `RUNTIME_REJECTED`，保持草稿 PR，并上传截图、UI 树和日志。取消或跳过时保留 `STATIC_VERIFIED_PENDING_RUNTIME`，Issue 标记为 `WAITING_RUNTIME`，不发表运行失败评论，也不晋级候选。PR 被改动时拒绝使用旧运行结果晋级。托管冒烟使用独立并发组串行执行，不取消正在执行的运行；排队行为由 GitHub Actions 控制。

`runtime-validation.yml` 可手动用官网最新且已登记的精确 APK 重放构建、静态检查、跨任务 artifact、托管冒烟和结果判定；不调用 Codex，不创建 PR 或修改正式支持清单。

环境版本、摘要、入口及证据说明见 [ReDroid 运行环境](../environment/redroid-runtime.md)。旧 `rebuild_ramdisk` 输入已移除。

会话记录在后台封存为独立 age 密文分段：32 条事件、1 MiB 缓冲或 15 秒间隔均可触发；后台调度繁忙时分段可能超过数量或大小阈值。协议分段不落地明文，封存加密不占用控制器读取事件的锁。
可信 action 在模型运行期间约每分钟上传 `impad-codex-live-*` artifact，包含本轮新增的完整密文分段与 runner 的内存、磁盘、进程名和 cgroup 计数；上传凭据不会传给模型。诊断采集与上传在后台执行，不要求 Codex 暂停，也不等待模型轮次完成；同一时刻仅运行一次诊断上传，较慢的上传会延迟下一次快照。
周期快照另外包含 `session-observation.tar.age`，使用 `CODEX_TRACE_RECIPIENT` 加密。归档保留原生会话目录、SQLite 状态及 Goal 数据库和最近 256 KiB 的 app-server stderr，排除认证、配置和符号链接。SQLite 使用在线备份；追加中的 JSONL 只保存采集开始时文件长度以内的完整行。`observation.json` 记录采集起止时间、thread ID、文件大小及省略的尾部字节数，并标注 `resumable: false`。不同文件的采集时刻可能不同，原生会话中尚未落盘的事件不会出现。
观察归档通过临时目录组装后加密，临时明文只存在于 runner 的私有临时目录中；上传完成后仍以现有的 turn 检查点作为自动恢复依据，观察归档不更新检查点指针。观察采集失败仅发出警告，Codex 继续运行。会话增长会增加压缩与上传开销；单份原生状态上限为 256 MiB，采集子进程最多运行 120 秒。
正常结束仍保留完整 `session.jsonl.age`。异常结束时按分段文件名排序分别解密即可恢复已上传的前缀，未形成完整分段或尚未上传的尾部可能丢失。
SIGTERM/SIGINT 会触发控制器诊断和加密收尾；SIGKILL、runner 丢失或平台终止整个 action 时，只能依靠此前已上传的快照，不保证最后一次上传成功。

JADX 启动器使用共享文件锁串行执行反编译，三个应用统一使用 2 GiB JVM 堆预算、反编译线程数默认为 2；直接调用 JADX Java API 也经过串行包装器并使用同一预算，避免多个反编译进程按整机内存比例同时分配堆。
静态分析另提供 DexSQL CLI `v0.1.1`，安装器按宿主系统和 CPU 选择官方二进制，并校验固定 SHA-256；同版本 `LICENSE` 和 `LICENSE-AUXILIARY` 一并校验、保留。安装目录只读挂载进模型沙箱，通过 `PATH` 调用，无需 MCP 配置。模型先以 `dexsql -t 2 query --input <apk> --sql <sql>` 筛选类、方法、字符串引用和调用关系，再用 JADX 阅读逻辑；关键 SQL、输出和输入 APK 哈希保存在 `work/analysis/dexsql/`。重复 `--sql` 可在同一次加载后查询；每个新 CLI 进程重新加载输入。线程数 2 是使用约定，不是进程内存上限；尚未测量 ARM64 CI 的内存收益。静态查询不能替代运行时自测和独立验收。
实时快照同时读取实际进程的 cgroup 内存计数，并尽力采集本轮内核日志和 runner 服务日志；服务日志可能包含凭据，只上传 `runner-logs.json.age` 密文，不上传原始内容。日志不可读时明确记录 `unavailable`。

适配 job 在 Ubuntu 24.04 ARM64 上先启动单个临时 ReDroid 14 容器（4 KB 页），用共用流程验证无模块时没有平板入口，并准备 Magisk、官方 LSPosed 和模块作用域。准备失败时不创建模型 Goal。
每轮生成临时 debug keystore，基线、模型构建和宿主验收通过 `WECHATPAD_DEBUG_KEYSTORE` 显式选择同一份密钥，避免不同 HOME 下默认 debug key 不一致；此密钥不用于正式发布。
运行报告从本次探测的新鲜 logcat 提取模块加载、兼容状态、Hook 安装及实际描述符，失败反馈直接提供这些观察。未观察到记录不等于证明注入失败；已安装 Hook 而入口缺失时，应重新核查所选方法的调用路径、提前返回和缓存。
模型沙箱通过工作区文件请求中转访问专用 ADB server，宿主控制器执行命令并返回原始输出；预检确认固定 ReDroid 端点可访问。中转固定设备、限制设备命令和本地文件路径，外部网络仍关闭。禁网沙箱禁止 Unix socket 的 connect 调用，因此不让模型直接连接 ADB socket。
模型使用 `adb wechat-launch` 在安装、清数据或重启后恢复受控启动设置并启动微信；设置和 Magisk 路径与独立冒烟共用。添加 profile 后，可用 `impad-build` 请求宿主执行已有改动范围保护、静态回归、测试和构建，日志写入 `work/analysis/host-build`。此命令不接受额外参数，不代替最终验收。
中转检测被取消的客户端请求并终止对应进程组；普通设备命令超时为 30 秒，传文件和等待设备为 120 秒，安装为 240 秒。独立 ReDroid 冒烟结束后，还验证受控启动、截图二进制输出和取消请求后的队列恢复，报告保存为 `tooling/tooling-report.json`。
同一个 Goal 每轮依次通过静态检查、测试、构建和 ReDroid 行为验收。运行验收安装可信控制器保存的精确候选 APK 和本轮模块，按应用策略验证 Hook；微信、企微验证平板入口和稳定二维码页面，QQ 验证 MSF 归属的手机／平板请求 AppID 对照；失败证据复制到模型工作区，交回原会话修正。更换证据目录不会重置连续三次同类错误的停止计数。
通过这轮验收后才创建草稿 PR，随后原有独立 job 在新的干净 ReDroid 再次冒烟，只有该独立结果可以晋级 `runtime-verified-hosted`。运行中的诊断继续加密保存，适配 ReDroid 的普通失败证据单独上传为各应用的 `adaptation-redroid` artifact。


### 可恢复的自动适配

各应用入口复用 `compatibility-app.yml`。每次先检查本仓库 GitHub Actions 机器人创建、仍打开的适配 Issue；仓库所有者的新非空评论优先作为输入，按评论 ID 顺序传入。已成功保存检查点且输入已送达模型的评论会记录游标，避免重复消费。没有新评论时走官方新版发现流程；手动关闭的旧任务不会被重新打开。

检查点 Artifact 保存候选报告、受允许的代码补丁、失败诊断、评论游标和加密的原生 Codex 状态。补丁和会话密文都有 SHA-256，恢复前验证候选身份和文件哈希。恢复原线程时同时输入最新评论和上次独立验收诊断；不能用协议 trace 替代原生状态。没有可恢复状态的旧任务只能创建新线程，并保留旧诊断。

原生状态仅保存 `sessions`、`archived_sessions` 和原生 `state_*`、`goals_*`、`thread_history_*` SQLite 数据库，排除认证、配置及符号链接，用 `CODEX_CHECKPOINT_RECIPIENT` 公钥加密，与现有会话日志的 `CODEX_TRACE_RECIPIENT` 分开。部署前需要设置匹配的 `CODEX_CHECKPOINT_IDENTITY` Secret 和 `CODEX_CHECKPOINT_RECIPIENT` Actions 变量；流水线会验证二者匹配。私钥仅由可信控制器使用，不传给模型。`CODEX_AUTH_JSON` 每轮另行注入并清理。Codex CLI 固定为已通过线程恢复测试的 `0.162.0-alpha.17.2`。

不设置 token 预算。每次显式执行一个模型 turn，由可信控制器独立验收；Goal 保持 paused，避免平台自动继续与控制器抢跑。未完成的代码和线程状态留给后续调度，运行仍受 GitHub job 超时约束。结果追加为 Issue 评论；独立托管运行时验收通过后自动关闭 Issue，失败则保持打开。
