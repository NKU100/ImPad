# 开发与兼容性维护

本地构建使用项目 Gradle Wrapper、JDK 21 和 Android SDK。提交审查前可运行与本轮交付相同的 Kotlin 检查及 Python 流水线测试：

```sh
./gradlew --no-daemon :compat-core:test :compat-rules:test :compat-checker:test :app:testDebugUnitTest :app:assembleDebug :app:verifyModuleMetadata
python3 -m unittest discover -s scripts -p 'test_*.py'
actionlint .github/workflows/*.yml
```

macOS 不提供仓库锁定的 Linux QQ DexKit native fixture。macOS 上因此无法完成的 QQ native fixture 测试必须记为未验证，不能通过伪造操作系统、改 skip 或把失败算作通过；Linux fixture 结果应由 Linux runner 单独记录。

## 当前三应用验收身份

微信 8.0.69 / 3040、QQ 9.3.1 / 14378、企业微信 5.0.8 / 70675 已使用同一份 GitHub 构建 APK 完成独立 ARM64 ReDroid 验收。完整行为、候选 APK 身份、tooling 与限制见[已验证版本与范围](support.md)和[三应用运行验证记录](validation/runtime-feature-matching-final.md)。

三次运行都引用构建 [39554279828](https://github.com/NKU100/ImPad/actions/runs/39554279828) 的 `impad-module-apk`，模块 APK SHA-256 为 `d8cdaf1aba2d19503c9a9a2679195456657faae1b5fae8e9d52e2c5ba1b88691`。构建提交为 `8690ed5162ba71c1d756f9e57793c774f2501de7`，127 项模块源码树 SHA-256 为 `654e389fd9b35a1362d6d9c79d2f07f8cd249f141fdfff59c74cff1b70f25aae`。三项运行分别是 [QQ 39555436990](https://github.com/NKU100/ImPad/actions/runs/39555436990)、[微信 39556863774](https://github.com/NKU100/ImPad/actions/runs/39556863774) 和[企业微信 39557803980](https://github.com/NKU100/ImPad/actions/runs/39557803980)。这些均是无账号验证；用户不会提供账号、扫码或提交登录。

## QQ collector 所有权合同

QQ 的验收复用历史自然请求采集合同：SYN 阶段用 PID、UID、精确进程名、process starttime 和 socket inode 确认 owner，并按 tuple generation 缓存。相同 tuple 和 ISN 的 SYN 只有在缓存 socket 与当前 inode/UID 可确认一致时才作为同 socket 重传；身份变化或缺失时重新解析，未知 owner 必须 fail-closed。MSF 请求仍须经过进程身份校验，非目标 flow 按已确认的 generation 过滤。

迁移验收不要求新增每包唯一 FD 扫描、FD handoff 跟踪或依赖 FIN 的关闭监听；不得把这些增量门槛重新加入，也不得据此放宽未知 owner、UID、进程名、starttime、包序列、流重组、capture 完整性、scope 或 AppID 检查。沿用该合同意味着 SYN 后的任意 FD handoff 未经过本合同验证；这是明确保留的局限，不能声称风险已消除。具体设计记录见[批准的统一设计](superpowers/specs/2026-10-21-wechat-rules-unification-design.md)。

## 构建与运行结果边界

QQ owner-contract restoration 的普通 Python 套件为386项、385通过、1项跳过。157项 JVM 测试因该轮未改 JVM 源码而复用之前结果，并非在该轮重新执行。随后 GitHub 构建 run 39554279828 独立执行完整 Python 测试（386项通过），Gradle 测试、模块元数据校验、三应用固定静态回归和 APK 构建均通过。每个检查的身份和结果见[三应用运行验证记录](validation/runtime-feature-matching-final.md)。

Oracle ARM64 ReDroid 的四轮运行均未通过；即使复测另一历史运行曾成功的精确 binary/APK 也未通过。Oracle framework 文件与权限一致性未经确认，因此现有证据只说明环境或部署差异仍待定位，不能归因为纯宿主问题。GitHub-hosted ARM64 ReDroid 上上述三次运行均通过；该结果不反向解释 Oracle 失败。

## 指定 APK 的候选检查

完整 `verify` 或 `adapt` 使用 GitHub Actions 中的 **Check WeChat compatibility**、**Check QQ compatibility** 或 **Check WeCom compatibility** 对应应用入口，再选择 `operation`。可填写精确 HTTPS APK 或 XAPK 下载直链 `apk_url`；留空时由该应用策略发现候选。手动 URL 允许第三方公共 HTTPS 下载源及查询参数；每次重定向都检查 HTTPS 和公共目标地址。自动发现仍限定官方来源。下载及 XAPK 解包均有 1.5 GB 上限；分包不合并、不重签，所有 APK 由共享检查器核对官方证书、包名、版本及 ABI。候选保存外层下载哈希与每个 APK 的哈希，独立验收整体安装并复核已安装分包哈希，不回退到自动发现的最新版本。可选的 `module_run_id` 和 `module_artifact` 仅用于 `verify`，留空时使用默认构建。`verify` 执行共享静态检查、固定代表回归和独立运行 gate，不启动模型、不创建 PR、不写正式验收表。`adapt` 仅在需要新规则时进入隔离适配；新规则使用新的不可变 ID 和临时路由，之后回到同一验证入口及完整 gate。GitHub Actions workflow dispatch 是生产级运行验收入口，本地候选检查不能代替它。

本地可用共享候选准备器检查指定 URL 并生成候选报告：

```sh
export APK_URL='https://<公共下载主机>/<精确 APK 或 XAPK 路径>'
python3 -m scripts.ci.candidate_selection \
  --app-id qq \
  --apk-url "${APK_URL:?请设置精确 HTTPS APK 或 XAPK 下载 URL}" \
  --directory work/candidates/qq
```

将 `qq` 换成 `wecom` 或 `wechat` 时，使用对应应用的官方 URL。此命令按显式候选调用共享准备/规则检查路径并保存产物；它不执行固定代表回归、Android 模块构建、独立运行验收或登记。不得把候选报告单独当作兼容性通过。

## 有界回归与身份

`python3 -m scripts.ci.static_regression --app-id qq`、`--app-id wecom` 和 `--app-id wechat` 分别使用该应用 acceptance 表中的固定代表，执行同一个规则 checker。旧 `targets.json` 和 profile 文件已从当前仓库树移除，历史版本可从 Git 历史恢复；它们不参与 schema 验证、候选选择或 APK 构建。常规回归集按首次成功验收代表固定并去重，不扫描历史 target/profile 数据。普通 verify/register 不提供首次迁移开关；缺失、空或损坏的 acceptance 表均失败关闭。

三个应用的运行规则、路由和策略随模块源码构建。运行时不加载 acceptance 表，也不使用其作为按 APK SHA 或历史 descriptor 准入的清单。Acceptance 只供固定静态回归和证据索引使用；所有应用的正式运行均使用共享规则解析、身份验证和策略绑定。

## 构建签名

本地 release 构建需在未纳入版本控制的 `keystore.properties` 中配置 `storeFile`、`storePassword`、`keyAlias` 和 `keyPassword`，或提供构建配置支持的签名环境变量。配置完成后执行：

```sh
./gradlew --no-daemon :app:assembleRelease :app:verifyReleaseModuleMetadata
```

不得把密钥或目标应用 APK 放入仓库。`THIRD_PARTY_NOTICES.md` 和模块 `assets/licenses/` 应与实际运行依赖及所分发许可证保持一致；更新依赖时重新核对依赖锁、原始许可证和 APK 内材料。

## 运行日志

- `NO_COMPATIBLE_RULE`、目标缺失或多解：检查精确 APK 身份、路由结果、规则目录和完整目标唯一性诊断。
- `CODE_INPUT_UNAVAILABLE`：必需 APK/split 代码输入不可读时模块失败关闭，不安装业务 Hook。
- 微信在 Tinker ready 后读取活动 ClassLoader 的 DEX 路径。当前支持包私有目录 `tinker/patch-*/dex/tinker_classN.apk` 的单个合并代码归档；存在多个活动归档或必需输入不可读时失败关闭，不按磁盘上最新的补丁目录猜测。
- 活动合并归档替代原 APK/split 作为微信规则的 DEX 搜索输入，已安装包的官方签名和身份仍独立校验。补丁内容哈希参与解析缓存和安装前后的一致性检查，避免补丁更新后复用旧描述符。
- 缓存命中只复用绑定了 APK/split、活动代码、规则、路由、策略和解析器版本的解析结果；完整成功仍须检查 Hook 安装日志及实际登录入口。

无账号运行验收不证明真实账号登录、服务端接受或双设备会话共存。Tinker readiness 观测不等于完整热补丁还原与匹配；QQ socket FD handoff 也未覆盖。
