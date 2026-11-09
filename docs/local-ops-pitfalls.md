# MemHub / Hy-Memory 本机运维踩坑（X2046 fork 运行）

面向本机 fork Runtime（build46 / 密封 `one-memory`）。公开上游 issue 仍走 GitHub；这里只记**本机反复踩过、且容易再犯**的坑。

更新：2026-11-09（Asia/Shanghai）

## 1. Core 带着 `ALL_PROXY=socks5://…` → 缺 `socksio` → probe ImportError

### 现象

- `memhub models test` / 激活立刻失败，日志类似：`model probe failed before dispatch: ImportError`
- 诊断里 probe 往往几十毫秒就 “completed”，CLI 却是 `model_probe_execution_failed`
- 直接 `curl http://127.0.0.1:3425/...` 可能完全正常

### 根因

- 本机 Clash 等会给 **用户域 launchctl** 写入 `ALL_PROXY=socks5://127.0.0.1:7897`（`launchctl getenv ALL_PROXY` 可见）
- MemHub.app 启动 Runtime/Core 时继承该环境；密封 Core **不带 `socksio`**
- httpx 见 SOCKS 代理就想加载 `socksio` → ImportError，被包成 probe 失败

### 错误做法

- **不要**往密封 Runtime 的 `core/_internal` 里塞 `socksio`：会触发 `installed_version_conflict` /「运行环境不一致」
- **不要**长期手改受管 LaunchAgent plist 塞代理（会 `runtime_service_file_modified`）

### 正确做法

1. 临时：`memhub stop` 后用清空代理的环境再 `memhub start`，确认 Core 进程 `ALL_PROXY` 为空，再 `models test` / `activate`
2. 持久：用树外包装启动 Core，包装内 `unset ALL_PROXY all_proxy`（以及 HTTP(S)_PROXY），再 `exec` 真正的 `one-memory`。见仓库旁本机脚本目录说明，或 `~/grok-exp/memhub-no-socks-core/`
3. 产品向根治（未做）：Core 启动时对本机回环忽略 SOCKS / 不 `trust_env` 盲跟 SOCKS

### 开机会不会复发？

**会。** 只要 login 后 launchctl 用户域仍有 `ALL_PROXY=socks5://…`，再开 MemHub.app，Core 又会带上 SOCKS。  
清空 plist 里的代理字符串不够：App 作父进程时仍继承用户域环境。必须靠 **Core 启动包装卸 SOCKS**，或改 Core 源码忽略 SOCKS。

---

## 2. Kuzu `graph` 文件损坏 → Core ready 后 SIGSEGV 崩溃循环

### 现象（2026-11-08 实际发生）

- `memhub status`：bridge 异常、全项未检查；无 `one-memory` 进程常驻
- `service-error.log` 反复出现新的 `Started server process [PID]`，PID 每 2~3 秒换一个
- `diagnostic-runtime.jsonl`：`core.ready` → ~0.7s 后 `core_exited_before_ready` / `SIGSEGV`
- `~/Library/Logs/DiagnosticReports/one-memory-*.ips` 堆积，faulting thread 在
  `_kuzu.cpython-312-darwin.so` → `PrimaryKeyIndex::load` ← `StorageManager::recover` ← `Database::Database`

### 根因

`~/.one-memory/hy-memory/governed-v2/graph`（Kuzu 单文件库）主键索引反序列化崩溃，多半是上次强杀/断电留下半个写。**与 ALL_PROXY 无关**——本次 launchctl 挂 SOCKS 但 LaunchAgent 已配 no-socks 包装，照样崩。

### 验证（不碰原文件）

```bash
cp ~/.one-memory/hy-memory/governed-v2/graph /tmp/g-test
python3.12 -m venv /tmp/kuzu-env && /tmp/kuzu-env/bin/pip install -q kuzu==0.11.3
/tmp/kuzu-env/bin/python -c "import kuzu; kuzu.Connection(kuzu.Database('/tmp/g-test', read_only=True))"
# 段错误 EXIT=139 → 确认文件本体坏，不是 Core 环境
```

注意：kuzu 与密封 Core 同版本（`kuzu-0.11.3`），系统 python3.14 无 wheel。

### 修复

```bash
memhub stop
mv ~/.one-memory/hy-memory/governed-v2/graph \
   ~/.one-memory/hy-memory/governed-v2/graph.corrupt-<date>-segfault
memhub start
```

hy_memory 缺文件会自动建空库（32768B 骨架）。**代价：图谱清零**，vector + sqlite 记忆仍在；图谱靠每日 13:00 evolve / `memhub evolve` 增量重建。损坏文件可保留做事后分析。

### 注意

- `graph-width` / `backup` 命令不管这种崩溃：doctor 在 Core 起不来时全项「未检查」，别指望它给修复建议
- 崩溃期诊断日志每 2.5s 一轮，先看 `diagnostic-runtime.jsonl` 的 `error_code`，再看 `DiagnosticReports` 最新 .ips 的 faulting thread 定库

## 3. 清理旧 Runtime 后 `sdk_migration_status_unavailable`

### 现象

- App 报记忆未迁移 / `sdk_migration_status_unavailable`
- `~/.one-memory/logs/sdk-migration.log` 出现 failed / frozen

### 根因（fork 私有 helper）

- fork Core 入口有 `keychain_helper_runtime`：校验一份**外部身份 Core**（路径 + sha256），再设置 `ONE_MEMORY_FROZEN_KEYCHAIN_HELPER`
- 清理旧 version 目录后，身份文件丢失或 hash 对不上 → 启动探测失败 → 写入 fence/blocked

### 做法

- 恢复/放置身份锚点（路径长度与嵌入字符串一致时可用原地替换；否则 remash，**解压后 marshal 体积不得超过原槽位**）
- 清掉 `~/.one-memory/.sdk-migration-capture-fence.json` 与 `sdk-migration-blocked.json` 后再 `memhub restart`
- 记忆数据在 `~/.one-memory`，清理 version 包不等于删库；仍勿对生产库做 `uninstall`

---

## 4. 钥匙串 / SecretStoreUnavailable

### 现象

- `credential_state=unavailable`；`models edit --api-key-file` 失败或 probe 报 `SecretStoreUnavailableError`
- 服务名一般为 `one-memory`，账号 `model-profile:<uuid>`

### 根因

- 写密钥走 frozen keychain helper；身份锚点若仍指向「已删签名 Core」或 helper 自己也无法启动，则 SecretStore 不可用
- 用官方 runtime 包中的 Core（无私有 helper 字符串）作为 `ONE_MEMORY_FROZEN_KEYCHAIN_HELPER` 目标时，需带齐同目录 `_internal`

### 做法

- 先保证 helper 链能跑（见上节），再 `models edit … --api-key-file`
- 紧急时可 `security` 写入同 service/account，但仍须 Core 能读钥匙串；ACL 过严时用 `-A` 仅作本机急救
- 历史教训：用 `uv`/python3.12 替密封 Core 会触发钥匙串 ACL 弹窗，勿默认这条路

---

## 5. 沉淀模型：用 `group/flash`，不要用 hy / hy4free

### 现象

- hy/hy4free：空 content、推理占满长度、结构化输出失败 → 激活被拒或写入 EMPTY_RESPONSE
- `disable_thinking=true` 对 hy4free 更容易空正文

### 本机约定（2026-11-08）

- Magpie：`http://127.0.0.1:3425/v1`，key 本地任意（如 `magpie`）
- 模型：**`group/flash`**（禁止 hy / hy4free）
- Embedding：本机 `local_onnx` / `bge-small-zh-v1.5`
- `disable_thinking=false`
- 验收：`memhub models test <profile-id>` 三阶段 model / structured_output / embedding 皆 ok，再 `activate --yes --wait`

---

## 6. 其它相关

| 坑 | 要点 |
| --- | --- |
| sync 开着时 resume/重启冒 history | 只砍非终态 history，不要全源 Freeze 挡新内容；见 issue #171 |
| Ultra `graph_build` 内存暴涨 | 建完若 footprint 十几 GB 且 CPU 已闲，优先 `memhub restart`，勿往密封树塞文件 |
| 公开 PR 勿带返工台账/费用 | 内部统计另存；仓内只留可公开工程内容 |

---

## 验收清单（改模型 / 重启后）

```bash
# Core 不得带 SOCKS
ps eww -p "$(pgrep -f '/current/core/one-memory' | head -1)" | tr ' ' '\n' | grep -i proxy

launchctl getenv ALL_PROXY   # 常为 socks5；有包装时 Core 进程仍应为空

memhub models list --json
memhub models test <profile-id> --json
```

## 4. 清理 grok-exp 时勿删例程依赖目录

### 现象（2026-11-09）

- 例程「MemHub 只砍 history」找不到 `~/grok-exp/memhub-no-history-guard/memhub-enforce-freeze.sh`
- 整夹在 2026-11-08 清理 grok-exp「施工垃圾」时被一并删掉；history 是否在跑变成未知

### 根因

- `~/grok-exp` 里既有一次性打包垃圾，也有**长期被例程/LaunchAgent 引用**的树外脚本
- 同类常驻依赖还包括 `~/grok-exp/memhub-no-socks-core/`（`ONE_MEMORY_CORE_COMMAND`）

### 正确做法

1. 清 grok-exp 前先对照例程与 LaunchAgent 路径：至少保留 `memhub-no-history-guard/`、`memhub-no-socks-core/`
2. 目录内应有 README 标明「例程依赖、勿清理」
3. 脚本丢了先停清扫，按「只砍 history、保留 sync」重写并补跑；不要默默跳过例程

### 改后证据

- 2026-11-09 重写恢复后补跑：`CHANGED=0 history_cancelled=0`，7 看护源 `sync_enabled=True`，Runtime 1.0.6 未重启

