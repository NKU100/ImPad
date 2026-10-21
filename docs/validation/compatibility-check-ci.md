# 兼容性流水线历史记录

本文记录 2026-10-21 微信规则统一之前的 profile/targets 流程，仅供追溯历史实现，不是当前操作手册。旧的候选窗口、descriptor checker、profile 建议和显式 LEGACY/RULES 分支均已退役；历史 `targets.json` 原字节保留，但不属于应用构建输入，也不由当前 runtime、checker 或 Actions 消费。

当前指定官方 APK 的 `verify` / `adapt` 入口、固定 acceptance 代表回归、可信 runtime verifier 与登记边界见[开发与兼容性维护](../development.md)。共享 `check-rules --app-id <id> --apk <apk> --report <json>` 是唯一静态 checker 入口，所有应用使用相同 candidate/report schema；应用专用策略只保留身份、版本路由和行为验收要求。

迁移前的运行范围与应用身份见[已验证版本与范围](../support.md)及 Git 历史。它们不替代当前 acceptance 记录，也不能作为不同 APK、不同模块字节或新流水线的验收结论。
