# 开发与兼容性维护

本地构建使用项目 Gradle Wrapper、JDK 21 和 Android SDK。提交审查前可运行与本轮交付相同的 Kotlin 检查及 Python 流水线测试：

```sh
./gradlew --no-daemon :compat-core:test :compat-rules:test :compat-checker:test :app:testDebugUnitTest :app:assembleDebug :app:verifyModuleMetadata
python3 -m unittest discover -s scripts -p 'test_*.py'
actionlint .github/workflows/*.yml
```

macOS 不提供仓库锁定的 Linux QQ DexKit native fixture。macOS 上因此无法完成的 QQ native fixture 测试必须记为未验证，不能通过伪造操作系统、改 skip 或把失败算作通过；Linux fixture 结果应由 Linux runner 单独记录。

## 指定 APK 的候选检查

完整 `verify` 或 `adapt` 使用 GitHub Actions 中的 **Run app compatibility pipeline**，选择 `app_id`、`operation`，并填写精确官方 APK `apk_url`。指定 URL 必须属于该应用策略允许的官方来源；候选准备按该 URL 固定 APK 字节，不回退到自动发现的最新版本。`verify` 执行共享静态检查、固定代表回归和独立运行 gate，不启动模型、不创建 PR、不写正式验收表。`adapt` 仅在需要新规则时进入隔离适配；新规则使用新的不可变 ID 和临时路由，之后回到同一验证入口及完整 gate。GitHub Actions workflow dispatch 是生产级运行验收入口，本地候选检查不能代替它。

本地可用共享候选准备器检查指定 URL 并生成候选报告：

```sh
export APK_URL='https://<该应用允许的官方主机>/<精确 APK 路径>'
python3 -m scripts.ci.candidate_selection \
  --app-id qq \
  --apk-url "${APK_URL:?请设置精确官方 APK URL}" \
  --directory work/candidates/qq
```

将 `qq` 换成 `wecom` 或 `wechat` 时，使用对应应用的官方 URL。此命令按显式候选调用共享准备/规则检查路径并保存产物；它不执行固定代表回归、Android 模块构建、独立运行验收或登记。不得把候选报告单独当作兼容性通过。

## 有界回归与身份

`python3 -m scripts.ci.static_regression --app-id qq`、`--app-id wecom` 和 `--app-id wechat` 分别使用该应用 acceptance 表中的固定代表，执行同一个规则 checker。历史 `targets.json` 仅作为原始历史资料保留；它们不参与 schema 验证、候选选择或 APK 构建。常规回归集按首次成功验收代表固定并去重，不扫描历史 target/profile 数据。普通 verify/register 不提供首次迁移开关；缺失、空或损坏的 acceptance 表均失败关闭。

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
- `NO_COMPATIBLE_RULE`、目标缺失或多解：检查精确 APK 身份、路由结果、规则目录和完整目标唯一性诊断。
- 缓存命中仅复用绑定了 APK/split、规则、路由、策略和解析器版本的解析结果；不能证明热补丁代码已读取或通过。

无账号运行验收不证明真实账号登录、服务端接受或双设备会话共存。当前验收也未覆盖完整热补丁还原与匹配。
