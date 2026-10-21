# 微信迁移与统一规则流水线

## 范围变更

本设计接续 2026-10-19 运行时特征匹配迁移：QQ、企微已完成本地交付，现在将微信加入迁移，最终只维护 RULES 运行与验证流程。保留原计划和历史验收证据，不能把扩展工作计入此前已完成的交付。

用户明确要求微信一起迁移，并只保留新的流水线。以下实施选择沿用原设计：微信首批基准为已有支持中最旧的 8.0.69（versionCode 3040），其他版本后续由指定官方 URL 验证；不扩大为全部历史版本。

## 微信接入

复用现有 RuleSession、路由、选择器、缓存、Android runtime 和原子安装。微信规则覆盖 tablet、login 两个必要目标；混淆类名和方法名不能由旧 JSON 直接照抄为特征规则。规则分析同时核对调用关系、方法形状及业务回调约束，静态唯一不等于行为正确。

使用现有 Tinker 就绪桥，沿用已验证身份与 Method/ClassLoader 检查。新规则和策略独立命名，QQ、企微既有规则、策略及首次验收代表保持不可变。微信通过独立静态及无账号运行验收后，建立首次 acceptance；不得继承旧 JSON 的已验收状态。

### 微信首次登记

首次登记是本次明确授权的微信迁移操作，由主代理审核完整证据后在本地执行，复用现有底层 snapshot、verify_validation_files 和 make_record API 的显式迁移能力，不为普通 CLI 或 workflow 重新开放 initial-migration 开关。仅接受 appId=wechat、固定官方 8.0.69 基准和原先不存在的微信 acceptance 文件；任一已有表（包括空表或损坏表）均停止首次登记，不覆盖、不修复性重建，也不以文件缺失自动触发迁移。

以同一份已校验快照执行原始证据重验，绑定精确 APK、最终模块、源码指纹、catalog、计划和持久化证据；空微信历史注明无历史回归样本，QQ/企微仍按既有登记执行回归。先生成 recordDraft，再由主代理核对并以不可覆盖的创建方式写入首条记录，最后用普通 loader 校验。该次操作和证据留档，不保留新的通用 bootstrap 命令。登记后所有日常 verify/register 固定非迁移模式，缺表、空表、损坏表继续拒绝；QQ/企微既有登记不改。

## 统一与删除

仍复用现有 workflow 文件及每应用入口，不另建一组平行 workflow。微信切换后，移除运行时、checker、候选准备、静态回归、适配提示词、登记和 workflow 中仅服务旧 profile 的分支，去掉 RULES/LEGACY 模式选择。应用策略继续表达包名、签名、进程、规则、策略及独立行为验收差异。

旧 targets JSON 只作为历史资料保留，不打包、不由正式运行和流水线读取。删除旧 descriptor installer、旧 cache/resolver、旧资产生成及仅被这些路径使用的依赖；仍被新流程使用的身份校验、下载、恢复控制、安全隔离、UI 验收和共享工具保留，不能仅因名称含 legacy 而删除。

迁移过程中允许微信暂时保留旧路径以便实施，但这不构成最终交付。最终不保留失配回退旧 JSON 的行为，也不保留可选 legacy 模式。

## 验证与交付

1. 微信最旧基准执行真实 Linux 静态查询，tablet/login 各唯一匹配；fixture 覆盖缺失、歧义和语义干扰。
2. 官方 APK 与原字节模块绑定，完成模块关闭/开启的入口、稳定二维码页面、Tinker/native/Hook 时序及生产 raw verifier 校验；不登录、扫码或宣称服务端登录成功。
3. 清理后的最终模块重新验收微信、QQ 与企微固定基准，不能复用不同 APK 字节的 runtime 结论。按已有规定执行有界静态回归，保护既有 acceptance 历史。
4. 执行受影响测试、构建、metadata、workflow 校验及残余引用审计，证明正式路径不再消费旧 profiles、targets 或模式开关。
5. Linux 静态与构建优先使用用户提供的 x64 DevCloud，ARM64 runtime 使用用户提供的 Oracle VPS 任务独占容器。不新增本机虚拟机；与正在进行的企微 5.0.12 验收隔离，不能竞争其设备或修改其冻结输入。

### QQ collector 验收补充（2026-10-21）

QQ runtime 继续复用既有自然请求采集、MSF socket/进程归属与 capture 完整性合同。collector 在 SYN 阶段必须通过 socket inode、UID、精确进程名、PID 与 process starttime 检查建立该 tuple generation 的归属缓存；只有精确 `com.tencent.mobileqq:MSF` owner 可进入请求事件，未知 owner 保持 fail-closed。已确认的非目标进程 flow 按该 generation 的缓存归属过滤，不增加每个 packet 的唯一 FD 重扫，也不新增 FD handoff 跟踪或依赖 FIN 的关闭监听。MSF 请求仍须执行原有进程身份校验；重复 SYN 只有在当前 tuple 的 socket inode 与 UID 均可读取且和缓存完全一致时才沿用 generation（同 socket 重传）；同 ISN 下身份变化、身份缺失或无法证明缓存 socket 仍相同时，必须重置并重新解析归属，未知结果继续 fail-closed。

每包 FD 唯一性与 FD handoff 不属于本次迁移验收增量。沿用历史缓存合同意味着 SYN 之后发生的 FD handoff 未经该合同验证；这是明确保留的既有局限，不得声称该风险不存在，也不得因此放宽未知 owner、UID、精确进程名、starttime、包序列、重组、丢包、partial、scope 或 AppID 校验。历史 GitHub run 39133831034 只证明其自身版本、APK、模块和运行环境，不替代本计划最终模块的 QQ runtime 验收。

企微 5.0.12 追加验收继续按现有冻结产物执行。微信扩展先完成设计检查与实施计划，再改产品代码。提交、推送、合并、发布与 GitHub Actions dispatch 仍需用户另行授权。
