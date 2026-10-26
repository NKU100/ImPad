# 自动适配的 Goal 与检查点

同一 Codex 线程持续完成适配。模型自行修改和测试；只有 Goal 标记完成时，控制器才执行独立验收。验收失败作为下一轮输入交回原线程。阻塞、执行中断与验收成功分别生成终态结果，没有自定义 token 或轮数上限。

每个 turn 结束后，控制器暂停 Goal，等待可信上传进程确认检查点。检查点包含代码 patch、加密原生会话、候选身份、基础提交和诊断。上传成功并发布恢复指针后才允许下一轮执行。上传失败不会继续适配或推进指针。收尾步骤复用已经发布的快照，避免将中断代码与旧会话组合。

任务初始化时保存已核验的候选输入报告，首轮检查点不依赖静态构建先成功。静态验证成功报告绑定基础提交与完整 patch 的 SHA-256；检查点只有在当前 patch 与该绑定完全一致时才保留静态成功阶段。再次验证开始或代码改变都会撤销旧验证状态。恢复到不同基础提交，或恢复没有绑定信息的旧检查点时，继续原模型会话重新处理。新增的当前应用和公共兼容核心测试随 patch 保存，已有测试仍须保持原内容不变。

首次候选在阻塞或中断时创建 Issue，成功无需创建。已有 Issue 的正文保留原结果，隐藏标记保存恢复元数据；每个 CI run 最多新增一个结果评论，重复发布更新同一评论。初次正文和评论使用同一模板。完成或阻塞的摘要由适配会话写入 `work/analysis/adaptation-summary.json`，包含 `completed`、`blockers`、`nextActions`、`questions` 四个字符串数组。脚本收集的验收事实决定终态，摘要不能修改验收结论。执行中断无需额外 AI 调用。

定时运行优先消费仓库所有者的新评论。没有新评论时发现新 APK；相同失败 APK 没有有效输入则等待。评论消费游标随已发布检查点更新。新候选遇到 runner 丢失时，下一次运行查找带候选 SHA 的已上传 Artifact，建立恢复 Issue 并等待输入，避免盲目重做。

live Artifact 的 `verified` 后缀只表示控制器验收完成，不表示 PR 发布或最终独立验收完成。没有对应 Issue 的检查点仍需建立恢复记录并等待所有者的新留言；不能仅凭后缀跳过候选。

恢复发布时先查找同候选的远端分支和 PR。未记录的分支必须与已恢复检查点的基础提交、代码树及受管提交身份完全匹配；未关联的 PR 还须来自对应检查点的适配运行。旧检查点的代码树随后续检查点保留，所有者修改补丁后仍能识别先前推送的分支。关联 PR 和 Issue 并保存精确 head 后，才以该 head 的 force-with-lease 更新分支。外部作者、其他候选或 Issue、不同 base、重复 PR 和并发 head 变化都会阻止更新。

PR 发布失败时保留中断状态、检查点指针和已消费评论游标，所有者的新留言仍能恢复同一 Issue。控制器的 `verified` 只能进入等待独立运行时验收的状态；只有最终运行时验收成功才能写入 `RUNTIME_VERIFIED` 并关闭 Issue。

恢复保证以成功上传的最后一份完整检查点为界，目标是最多丢失当前未持久化的 turn。Artifact 过期、删除、解密密钥不可用或恢复校验失败会阻止自动恢复，不能承诺永久保留。原生会话与 Goal 续轮的实际协议行为曾由真实 Codex CI 冒烟验证；本地控制器单元测试不构成运行时证明。

通用流水线按应用和操作分组：同一应用的适配共用 `impad-compatibility-<app>-adapt`，不同应用可并行；验证任务使用各自的 run ID。顶层队列使用 `queue: max`，避免多应用适配互相取消等待任务。只有 Codex 适配 job 绑定固定 GitHub Environment `codex-managed-auth` 并获取 `impad-codex-managed-auth` 全局锁；该锁覆盖认证恢复、模型执行及刷新认证写回，使用 `queue: max`，运行中的任务不会被取消且等待任务保留。认证从该 Environment 的 `CODEX_AUTH_JSON` secret 读取，写回也仅更新该 Environment。适配预检用写回令牌通过 Environment secret 元数据 API 确认 `CODEX_AUTH_JSON` 存在，并探测该 Environment 的 public-key API 权限；API 只返回元数据和公钥，不读取 secret 值。缺少 Environment secret、元数据不可读或无法取得 Environment public key 均会明确失败，repository 同名 secret 不作为回退。`CODEX_AUTH_WRITE_TOKEN` 必须具备 Environments 读写权限；仅有 repository Secrets 写权限不足。迁移前须先在 `codex-managed-auth` Environment 初始化 `CODEX_AUTH_JSON`，之后适配 job 才能从 Environment 读取并在锁内写回刷新认证。微信、企微和 QQ 的定时／手动入口调用 shared workflow；shared 与 runtime-validation 均只由 `workflow_call` 触发。
