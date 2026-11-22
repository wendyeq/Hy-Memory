# Agent notes（本 fork 工作区）

## 本机运维踩坑

同步上游，或升级、安装本机 MemHub Runtime 时，先读 [升级 SOP](docs/memhub-upgrade-sop.md)。那份文件是唯一流程正文。

改 MemHub Runtime、模型配置、钥匙串、清理 version 目录前，先读：

- [docs/local-ops-pitfalls.md](docs/local-ops-pitfalls.md)

其中最重要的两条：

1. **不要**在 `ALL_PROXY=socks5://…` 下对密封 Core 做 `models test`/激活；缺 `socksio` 会 ImportError。开机后 launchctl 用户域常仍有 SOCKS，**会复发**，靠 Core 启动包装卸代理。
2. 沉淀模型用 **`group/flash`**（Magpie `127.0.0.1:3425/v1`），不要用 hy / hy4free；探测前确认 Core 进程代理已清空。

## 仓规提醒

- 公开提交/PR 不要带返工台账、费用、个人绝对路径、密钥。内部报告、会话复盘若已误提交且尚未推送，不要只在最新提交里删除文件：从干净合并点另开待发布分支，重放必要提交，使待发布历史中不再含这些文件。
- 升级前先盘点实际在跑的树外和密封私有补丁，不要只对照 git main。
- 架构边界见 `memhub/MEMORY.md` 与 `memhub/docs/ARCHITECTURE.md`。

清理 `~/grok-exp` 时勿删 `memhub-no-history-guard/` 与 `memhub-no-socks-core/`（例程/LaunchAgent 依赖）。详见 `docs/local-ops-pitfalls.md` §4。
