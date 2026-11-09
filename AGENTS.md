# Agent notes（本 fork 工作区）

## 本机运维踩坑

改 MemHub Runtime、模型配置、钥匙串、清理 version 目录前，先读：

- [docs/local-ops-pitfalls.md](docs/local-ops-pitfalls.md)

其中最重要的两条：

1. **不要**在 `ALL_PROXY=socks5://…` 下对密封 Core 做 `models test`/激活；缺 `socksio` 会 ImportError。开机后 launchctl 用户域常仍有 SOCKS，**会复发**，靠 Core 启动包装卸代理。
2. 沉淀模型用 **`group/flash`**（Magpie `127.0.0.1:3425/v1`），不要用 hy / hy4free；探测前确认 Core 进程代理已清空。

## 仓规提醒

- 公开提交/PR 不要带返工台账、费用、个人绝对路径、密钥。
- 架构边界见 `memhub/MEMORY.md` 与 `memhub/docs/ARCHITECTURE.md`。

清理 `~/grok-exp` 时勿删 `memhub-no-history-guard/` 与 `memhub-no-socks-core/`（例程/LaunchAgent 依赖）。详见 `docs/local-ops-pitfalls.md` §4。
