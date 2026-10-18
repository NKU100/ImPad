# 开发与兼容性维护

本地使用 JDK 21、项目所需 Android SDK 和仓库 Gradle Wrapper 构建：

```sh
./gradlew --no-daemon :app:assembleDebug :app:verifyModuleMetadata
python3 -m unittest discover -s scripts -p 'test_*.py'
```

本地 release 构建需要在未纳入版本控制的 `keystore.properties` 中配置 `storeFile`、`storePassword`、`keyAlias` 和 `keyPassword`，或提供构建配置支持的签名环境变量。配置完成后执行：

```sh
./gradlew --no-daemon :app:assembleRelease :app:verifyReleaseModuleMetadata
```

三应用静态回归命令：

```sh
python3 -m scripts.ci.static_regression --all-apps
```

回归分别从微信、QQ、企业微信的正式 profile 中选择已通过运行验证的最近三个不同 versionCode，保留这些 versionCode 下的全部 APK 变体。每个应用使用独立缓存和报告；某个构建下载或检查失败后继续检查其余构建和应用，汇总有失败时命令以失败退出。报告位于 `work/static-regression/`。

`Build module APK` 工作流执行单元测试、模块构建及上述三应用回归。三个应用的兼容性入口分别调用适配流水线；运行验收由独立 ReDroid 环境执行。静态回归不能替代最终模块 APK 的运行验收。流程及证据要求见[兼容性检查 CI](validation/compatibility-check-ci.md)。

使用 `Validate ImPad runtime` 手动验收已有模块产物时，填写 `module_run_id` 和 `module_artifact`，以复用该次构建的原始 APK，避免重新构建 debug。验证登记构建需明确选择 `candidate_mode=registered`；同版本有多个 APK 变体时还需填写 `apk_sha256`。留空 `module_run_id` 会构建 debug 重放模块，默认候选模式为 `latest`。

构建与签名规则：

- main 和 `v*` tag 推送默认构建稳定签名的 release，`debuggable=false`；release 缺少稳定密钥时失败。
- main／版本 tag 上手动构建默认 release，也可选择使用同一稳定签名的 debug。
- PR 和临时分支推送构建 debug，不注入稳定签名 Secrets。临时分支手动构建只允许 debug；请求 release 会失败。
- debug 和 release 均注册微信、QQ、企业微信适配器；实验规则仅在 debug 中加载。

CI 模块产物包含 APK、对应提交的源码归档、`SHA256SUMS`、签名信息和构建记录。版本 tag 必须与 APK 版本一致，格式为 `v<versionName>`。正式发布使用稳定签名的 release APK，并附对应源码及校验和。


## 运行日志

- `UNKNOWN_BUILD`：当前安装包未匹配正式 profile，应核对包名、版本、ABI、APK SHA-256 和签名摘要。
- `COMPATIBLE`：兼容性解析通过，仍需检查当前进程的 Hook 安装日志。QQ 相关行为位于 `:MSF` 进程。
- `cacheHit`：复用了兼容性解析缓存，不能单独证明运行行为。

debug／release 与签名方式独立；debug APK 也可使用稳定签名。覆盖安装取决于包名、签名及 Android 的版本检查。
