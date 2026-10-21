# ReDroid 运行环境

自动适配、手动重放和最终独立验收统一使用 Ubuntu 24.04 ARM64 上的 ReDroid 14。配置集中在 `scripts/ci/redroid_environment_probe.py`：容器镜像按 SHA-256 固定，Magisk 31.0、Zygisk Next 1.5.2 和 LSPosed 2.2.1 的输入同样校验摘要。Android 客体为原生 ARM64、4 KB 页，无 ARM 指令转译和 AVD/KVM 依赖。

## 入口

- `compatibility-app.yml`：仅由三个应用入口调用；适配 job 启动独立容器，验证无模块基线和框架后才启动模型。每轮构建通过后安装精确候选与本轮模块，独立验证行为；失败证据交回同一会话。
- `runtime-validation.yml`：统一手动／自动验收入口；手动选择版本并构建模块，自动调用复用候选 artifact 和模块 artifact；重新校验候选摘要、签名和静态规则后在新容器执行验收。

`environment/redroid-smoke/` 中保留迁移前的运行样例供追溯，不作为当前流水线输入。三个应用的候选均从共享 `PREPARED` 报告、静态报告、APK 输入清单和构建时模块身份准备；不会读取或生成 `targets.json`、旧 profile 或 debug-only 本地 target assets。候选准备不授予正式支持；登记仍依赖独立验收及可信登记入口。

## 主机工具与隔离

Google SDK 的 Linux 原生构建工具通过主机 qemu-user 执行 x86_64 二进制，配置脚本为 `scripts/ci/prepare_arm64_sdk_host.sh`；这只影响宿主构建工具，Android 和 QQ 库仍原生运行。设备通信显式使用 `/usr/bin/adb` 的 ARM64 版本。

容器端口只绑定 `127.0.0.1:16555`，转发客体 5555。适配控制器建立专用文件系统 ADB socket，只允许这一设备；模型通过文件请求中转访问，外部网络仍关闭。实际运行还检查受控启动、PNG 二进制输出和取消请求后的队列恢复。

容器需要 binder 内核模块、Docker privileged 和抓包所需 netfilter 模块。控制器负责启动、重连、采集日志和删除容器；不使用 VPS 传输或实体手机。

## 验收与证据

微信和企微要求模块关闭时没有平板入口，模块开启时进入稳定二维码页，并由当前应用进程的新鲜日志证明兼容与 Hook 安装。QQ 要求 Manifest 派生的两态 AppID、MSF socket 归属、完整 TCP 流和当前 owner 的兼容／Hook 日志，丢包、缺失归属或不完整数据均不能通过。全程不输入账号、扫码或提交真实登录。

单候选标准报告为 `work/runtime-smoke/evidence/smoke-report.json`。应用目录保存 UI 树、截图和请求元数据；框架目录保存启动日志，`memory-samples.json` 记录采样峰值，`tooling/tooling-report.json` 保存中转验证。准备或运行失败保持待验证／拒绝状态，跳过 job 不算通过。历史 AVD 验证文档仅作参考，旧镜像、ramdisk 和启动脚本由 Git 历史保留。
