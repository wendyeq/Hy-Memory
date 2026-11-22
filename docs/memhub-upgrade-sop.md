# 升级本机 MemHub fork

同步上游，或在本机安装、升级由本 fork 打出的 MemHub Runtime 时，只按本文做。踩坑现象、根因和修复见 [local-ops-pitfalls.md](local-ops-pitfalls.md)。

下面的命令由你在本机执行。普通技术步骤做完就进入下一步。权限、模型切换和会产生费用的动作必须停下，见下文。

官方安装合同见 [INSTALLATION.md](../memhub/docs/INSTALLATION.md)。备份目录里有什么、停写时如何保持 Chroma 与 Kuzu 一致，见 [backup-v2.md](../memhub/docs/contracts/backup-v2.md)。命令行以 `memhub/apps/cli/src/command-catalog.ts` 为准。

## 变量

先给下面这些名字赋值。不要把某一次的版本号、build 或提交写进命令来代替它们。版本和 `build_number` 以 `$SHA` 上的 `memhub/package.json` 里 `version` 与 `release.build_number` 为准。

- `$UPSTREAM_TAG` 是要吸收的上游正式 tag，不带 `refs/tags/` 前缀。
- `$SHA` 是最终要安装的完整 git 提交。
- `$ARCH` 是本机原生架构，`arm64` 或 `x64`。构建机上 `node -p process.arch` 的结果必须等于 `$ARCH`。
- `$OUTPUT_DIR` 是这次构建的输出目录。它必须尚不存在。
- `$INSTALL_ROOT` 是受管安装根。macOS 产品默认值是 `$HOME/Library/Application Support/One Memory`。
- `$DATA_DIR` 是数据目录。产品默认值是 `$HOME/.one-memory`。App 在未设置 `ONE_MEMORY_DATA_DIR` 时用这个默认值。从第 1 步起，若环境里有 `ONE_MEMORY_DATA_DIR`，它必须等于 `$DATA_DIR`。没设置时，`$DATA_DIR` 必须是 `$HOME/.one-memory`。只给 `install` 或 `upgrade` 传 `--data-dir` 不会让后面的 `backup`、`status` 和 `recall` 换到另一套数据。对不上就停下。
- `$APP_DEST` 是本机已经在打开的 `MemHub.app`。
- `$BACKUP_NAME` 是不带 `.memhub-backup` 后缀的新名字。执行前，`$DATA_DIR/backups/$BACKUP_NAME` 和 `$DATA_DIR/backups/$BACKUP_NAME.memhub-backup` 都不存在。
- `$RECALL_QUERY_FILE` 是一个普通文件的路径。文件内容是一条你本来就知道能召回的查询。不要把查询写进 shell 历史。

## 先停下来的情况

下面任何一条成立，就停下，不要改命令绕过去。

- 步骤会弹出钥匙串、签名或其他新权限。
- 步骤会更换正在使用的生成模型、向量模型或记忆模式。`memhub models activate`、`memhub models add`、带新模式的 `memhub settings mode`、`memhub settings backends set` 都属于这一类。
- 步骤会调用计量或付费模型。`memhub models test` 也算。先得到明确同意，再用盘点里已经在用的 profile id。不要把密钥写进文档、工单或共享日志。
- 要打 `--mode public` 的包，但环境里还没有 `CSC_NAME`、`CSC_KEYCHAIN`、`MEMHUB_PUBLIC_BUNDLE_ID`。不要把这三个值写进命令记录。

## 哪些步骤会动本机 Runtime

这些步骤不停止、不替换正在服务的受管 Runtime。盘点、对照 tag、隔离测试、构建，以及不带 `--yes` 的 `install` 或 `upgrade` 预览。预览失败也不改 `current`。隔离测试会写自己的报告。构建会写入 `$OUTPUT_DIR`。

`memhub backup create --target "$DATA_DIR/backups/$BACKUP_NAME" --yes` 会短暂停写，服务保持运行。它暂停 SDK 写入，关掉客户端后再复制本机 Chroma 与 Kuzu，然后恢复写入。

这些步骤会停止或替换正在运行的 Runtime。带 `--yes` 的 `install` 或 `upgrade`、用新 App 替换 `$APP_DEST` 并打开它、`memhub stop`、`memhub start`、`memhub restart`、`memhub backup restore`。已有旧 Runtime 时，除了下面为备份而做的那一次启动，它们都放在备份完成之后。

`memhub status` 不启动服务。Runtime 没在跑时，`memhub doctor` 以退出码 3 结束。

## 1. 盘点正在运行的树

对照实际进程和已加载的服务，不要只对照 git main，也不要只看文件在不在磁盘上。树外包装、密封身份 helper、history 窗口和 SOCKS 守护都可能只以文件形式存在，而没有接到当前进程。

在 fork 检出目录记录 `git rev-parse HEAD` 和 `git status`。这是源码树，不是正在服务的那一棵。

先看有没有已安装的 Runtime。`readlink "$INSTALL_ROOT/current"` 必须失败。`$INSTALL_ROOT` 是产品默认值时，`$HOME/Library/LaunchAgents/com.tencent.onememory.runtime.plist` 也不存在。`$DATA_DIR` 同样不存在。三件都成立，才是首次安装。记下「无现有 Runtime、无旧数据」，跳到第 2 步。不要为了盘点去创建数据目录。`$INSTALL_ROOT` 不是产品默认值时，服务文件不叫这个名字。`current` 不存在还不能当成首次安装，先停下核对服务。`$DATA_DIR` 已经有数据，但找不到旧 Runtime 时，也停下。

旧 Runtime 已核对、但 `memhub status --json` 显示它没在跑时，执行一次 `memhub start`。这是备份前唯一允许的启动。启动后再做下面的只读命令。

只读命令：

```bash
memhub status --json
memhub doctor --json
memhub jobs list --json
memhub sync status --json
memhub sources list --json
memhub models list --json
memhub settings mode --json
memhub settings backends show --json
readlink "$INSTALL_ROOT/current"
launchctl getenv ALL_PROXY
```

`settings mode` 不要带 `lite`、`pro` 或 `ultra`。`settings backends` 用 `show`，不要 `set`。

从 `jobs list` 抄下每条任务的 `job_id`、`operation` 和状态。从 `sync status` 抄下哪些来源开着同步。这一步不要 `pause`、`resume`、`cancel`，也不要 `jobs start`。

再看两处，不要看进程路径像不像包装。包装 `exec` 真正的 `one-memory` 之后，进程路径就是密封二进制。接线生效是指已加载的 LaunchAgent 的启动命令仍指向包装，并且这个 Core 进程的代理变量为空。只记录启动命令是否指向包装，以及代理变量是否为空。不要把 EnvironmentVariables 或其他环境写进笔记，里面可能有密钥。文件在旁边的目录里，不算接线生效。现象和错误做法见 [local-ops-pitfalls.md](local-ops-pitfalls.md) 里「Core 带着 `ALL_PROXY`」一节。

身份 helper 同样只认当前启动链。不要记录钥匙串哈希或身份锚的秘密。若 doctor 或日志已经是 `sdk_migration_status_unavailable`，或凭据状态不可用，先按踩坑文档里「清理旧 Runtime 后 `sdk_migration_status_unavailable`」和「钥匙串」两节处理，不要进入安装。

history 行为以 `$SHA` 的代码为准，不把某一个天数写成以后的标准。符号是 `memhub/apps/core/src/one_memory/api.py` 里的 `RESUME_HISTORY_WINDOW` 和 `ONBOARDING_HISTORY_WINDOW`。本 fork 要保住的行为是启动和同步恢复不继续 history，只走增量 sync。

完成条件是笔记写明正在服务的 `current` 目标，每个私有 helper 是已加载还是只有文件，每条任务的 `job_id`、`operation` 和状态，以及已开启 sync 的来源。首次安装则写明没有这些东西。

## 2. 对照上游 tag，只留最小补丁

```bash
git fetch upstream tag "$UPSTREAM_TAG"
git diff "$UPSTREAM_TAG" -- memhub
```

只保留盘点证明正在生效、且上游 tag 里没有的改动。本 fork 留下的是 history 窗口，以及启动和 sync resume 不继续 history。回归测试是 `memhub/apps/core/tests/test_resume_history_window.py`。树外 helper 留在树外，不要为了升级把它们提交进仓库。上游已经有的补丁不要再叠一遍。

若当前 HEAD 已经是要安装的树，确认上面的 diff 只含这些补丁，把 `git rev-parse HEAD` 记为 `$SHA`。工作区要干净。

若还要吸收 `$UPSTREAM_TAG`，在干净分支上执行 `git merge "$UPSTREAM_TAG"`。冲突时只留下上一节决定保留的补丁。合并完成且工作区干净之后，把 `git rev-parse HEAD` 记为 `$SHA`。不要改写已有历史。

完成条件是 `git rev-parse HEAD` 等于 `$SHA`，工作区干净，并且相对 `$UPSTREAM_TAG` 的 diff 里只剩下你决定留下的补丁。

## 3. 在最终 SHA 上做聚焦测试

在 `memhub` 目录执行。隔离运行器使用自己的 home 和端口，脚本会把 `ONE_MEMORY_DATA_DIR` 指到临时目录，不要再改成生产用的 `$DATA_DIR`。不要跑 `core-all`、`workspace` 或整份 Core 套件。

`core` 模式在 `apps/core` 里启动 pytest，所以测试路径相对那个目录。

```bash
bash scripts/run-tests-isolated.sh core tests/test_resume_history_window.py
bash scripts/run-tests-isolated.sh node --test scripts/platform/macos/macos-release-scripts.test.mjs
```

两条都结束后再看一次 `git rev-parse HEAD`。

完成条件是两条命令退出码都是 0，且 HEAD 仍是 `$SHA`。

## 4. 打出与该 SHA 一致的 App 与 Runtime

在 macOS 上、且 `node -p process.arch` 等于 `$ARCH` 时构建。输出目录必须尚不存在。日常用 `internal`。只有环境里已经有签名身份时才用 `public`，否则停在「先停下来的情况」。

```bash
node scripts/platform/macos/build-macos-release.mjs \
  --output "$OUTPUT_DIR" \
  --arch "$ARCH" \
  --mode internal
```

脚本参数只接受 `--output`、`--arch`、`--mode`。`git_commit` 来自构建时的 `git rev-parse HEAD`，不是手写进包里的。

构建成功后确认三件事。命令退出码是 0。`$OUTPUT_DIR/macos-build-receipt.json` 的 `result` 是 `passed`，且 `same_origin_checks` 里每一项都是 `pass`。`$OUTPUT_DIR/memhub-runtime-darwin-$ARCH/manifest.json` 的 `git_commit` 等于 `$SHA`。

完成条件是 App、独立 Runtime 和收据都在 `$OUTPUT_DIR`，并且 manifest 的提交就是 `$SHA`。同号的上游 Release 不是这个包。不要改用 `npm install -g` 再让 CLI 去下载远程 Release。

## 5. 做可成对回滚的备份

首次安装没有旧数据，跳过这一步。

已有 Runtime 时，先确认第 1 步观测到它正在运行。这一步会短暂停写，不会停服务。

把 `readlink "$INSTALL_ROOT/current"` 的结果记为 `$PREVIOUS_RUNTIME`。这一步先不退出 App，也不复制 `$APP_DEST`。要不要替换 App，要等第 6 步的预览结果。

```bash
memhub backup create --target "$DATA_DIR/backups/$BACKUP_NAME"
memhub backup create --target "$DATA_DIR/backups/$BACKUP_NAME" --yes
```

第一条只显示预计大小，不写备份。第二条才写入。成功后的目录是 `$DATA_DIR/backups/$BACKUP_NAME.memhub-backup`。后面的检查和恢复都用这个最终路径，不要用输入名。

打开最终目录的 `manifest.json`。`schema_version` 不是 2 就不要继续安装。备份里要有 `sdk/governed-v2`，其中包含本机 Chroma 与 Kuzu，而不是只有管理库 sqlite。`backend` 要与第 1 步的 `settings backends show` 一致。把 `runtime_build`、最终备份路径和 `$PREVIOUS_RUNTIME` 记在一起。

这是一对记录。只有 CLI 的 `upgrade` 在迁移开始前失败时，才会自己把 `current` 恢复到 `$PREVIOUS_RUNTIME`。App 路径不要假定也会自动恢复。迁移开始之后，不要把 `current` 指回旧目录，也不要打开 `$APP_PREVIOUS`。数据恢复只用文末的 `backup restore`，它不切换 Runtime。

完成条件是最终备份目录存在，manifest 为 schema 2，SDK 树在备份内，并且笔记里的 `$PREVIOUS_RUNTIME` 与这份最终路径写在一起。

## 6. 用官方入口安装

从这里开始会替换正在运行的服务。使用刚打出的 Runtime 里的 CLI，让 CLI 和包是同一次构建。不要省略 `--source`，避免 CLI 去访问网络。

首次安装没有 `current`。预览用 `install`，不要用 `upgrade`。

```bash
"$OUTPUT_DIR/memhub-runtime-darwin-$ARCH/bin/one-memory" install \
  --source "$OUTPUT_DIR/memhub-runtime-darwin-$ARCH" \
  --install-root "$INSTALL_ROOT" \
  --data-dir "$DATA_DIR"
```

预览退出码是 0 且 `apply` 为 false 时，同一条命令加上 `--yes`。不要加 `--allow-downgrade`。

已有 Runtime 时，预览用 `upgrade`。它不停止服务。

```bash
"$OUTPUT_DIR/memhub-runtime-darwin-$ARCH/bin/one-memory" upgrade \
  --source "$OUTPUT_DIR/memhub-runtime-darwin-$ARCH" \
  --install-root "$INSTALL_ROOT" \
  --data-dir "$DATA_DIR"
```

然后只走匹配的一支。

若构建号高于当前 `current`，且预览退出码是 0，`apply` 为 false，再执行同一条命令并加上 `--yes`。这会切换受管版本并重启服务。不要加 `--allow-downgrade`。

若同 build 且字节不同，预览在 stderr 的 JSON 里给出 `installed_version_conflict`，退出码非 0，`current` 不变。`upgrade` 的参数里没有同 build 替换开关。不要对已安装目录改字节后再重试，也不要改用 `upgrade --yes` 硬装。

这时只有打包 App 能替换同 build。它不读你在 CLI 里传的 `--install-root`，数据目录也不跟着这条 CLI 走。仅当 `$INSTALL_ROOT` 等于 `$HOME/Library/Application Support/One Memory`，且 `$DATA_DIR` 等于 `$HOME/.one-memory`，并且没有把 `ONE_MEMORY_DATA_DIR` 指到别处时，才继续。替换之前先读当前 `current` 和新 Runtime 两份 `manifest.json` 的 `git_commit`。两份都是 40 位小写十六进制才算满足格式。当前 manifest 读不出来，或任一提交不是这个格式，停下来。当前安装树对不上自己的 manifest 时，也不要按本文继续。

格式满足后，先退出旧 App。把 `$APP_DEST` 复制到安装根和数据目录之外，记为 `$APP_PREVIOUS`，确认副本存在。不要复制进 `$INSTALL_ROOT/versions`。再用 `$OUTPUT_DIR/MemHub.app` 替换 `$APP_DEST`，然后打开 `$APP_DEST`。格式满足不代表替换已经成功。打开后核对新的 `current` manifest，`git_commit` 必须是 `$SHA`。若仍报 `installed_version_conflict`，记下两份 `git_commit` 和错误后停止。不要改安装目录里的文件。旧的版本目录仍留在 `$INSTALL_ROOT/versions`。

安装根或数据目录不是上面的产品默认值时，不要替换 App。同 build 冲突没有别的产品入口，停下来。

若字节相同的同 build，预览退出码是 0，且 stderr 没有 `installed_version_conflict`。不要再执行 `upgrade --yes`。当前 `current` 的 `git_commit` 已经是 `$SHA` 时，直接进入验收。

若构建号更低，预览给出 `distribution_downgrade_refused`。停止。这不是这次升级的路径。

完成条件是下面三者之一。首次 `install --yes` 之后，`current` 的 manifest `git_commit` 是 `$SHA`。升级后 `current` 指向新版本目录，且其 `git_commit` 是 `$SHA`。同 build、字节相同，且现有 `current` 已经核对为 `$SHA`。

## 7. 验收

通过标准不是健康检查返回 HTTP 200。

服务起来之后，先看新的 Core，不要用第 1 步的旧进程记录代替。已加载的 LaunchAgent 仍应指向盘点时的包装。Core 进程的代理变量必须为空。不要用进程路径是否等于包装来判断。Core 进程带着 SOCKS 时，不要做 `models test`，也不要把验收记为通过。包装和代理的判据见踩坑文档里 SOCKS 那一节。

然后做只读核对。不要换模型，也不要新开 history。`$RECALL_QUERY_FILE` 必须在跑命令前就写好。首次安装没有旧记忆时，跳过 `recall`，并在笔记里写明没有旧查询。

```bash
memhub models list --json
memhub settings mode --json
memhub settings backends show --json
memhub sources list --json
memhub sync status --json
memhub jobs list --json
memhub recall --query-file "$RECALL_QUERY_FILE" --json
```

冷启动会停服务。做完上面的只读核对再做。首次安装也做一次，用来看 helper 是否还在。

先记下当前 Core pid。执行 `memhub stop`。身份不明确时，CLI 不发停止信号，所以不能只看命令退出。确认这个旧 pid 已经不在进程表里，再执行 `memhub start --json`。旧 pid 还在就停下，不要把后面的检查做在旧进程上。

```bash
memhub stop
memhub start --json
```

`start` 返回后立刻记下新的 Core pid。它必须是新进程，不能还是停止前那个 pid。再看 LaunchAgent 的启动命令和这个进程的代理变量。若盘点时包装已加载，启动命令仍指向那个包装。若盘点时没有包装，而 `launchctl getenv ALL_PROXY` 非空，不要把冷启动记为通过。用户域即使仍是 SOCKS，Core 进程里的代理变量也要为空。身份 helper 仍能让 `models list` 可读。做不到时不要改密封树，回到踩坑文档里 SOCKS、清理旧 Runtime 和钥匙串那几节。

隔两分钟再执行 `memhub status --json`，并再取一次 Core pid。pid 与 `start` 刚结束时相同，才算这段时间没有换进程。pid 变了就看 `memhub logs --errors-only --since 5m`，不要判为稳定。崩溃循环按踩坑文档里 Kuzu `graph` 一节处理。不要用健康检查接口代替这次观察。

再跑 `memhub jobs list --json` 和 `memhub sync status --json`。用 `job_id` 对照第 1 步。启动没有新建 `operation` 为 `history` 的任务，也没有把盘点时那些 history 的 `job_id` 推进成全量回补。原来开着 sync 的来源仍开着，只允许 sync 任务增量前进。首次安装应没有 history 任务。不要执行 `sources connect --import`，也不要 `jobs start --operation history`。

生成模型连通性放在冷启动和代理核对之后。它会调用现用模型，可能产生费用。先停下并得到同意，再对 `models list` 里当前 profile 执行 `memhub models test <profile-id> --json`。不要 `activate`，不要 `add`，不要改模式。没有同意就不要跑。已有 profile 但没有成功结果，验收未完成。首次安装确实没有现用生成模型时，不要为了验收新增模型，把 `models test` 记为不适用。

完成条件是这些同时成立。现用模型列表、模式和存储后端与盘点一致。首次安装没有 profile 时，列表为空，且 `models test` 记为不适用。已有 profile 时，同意之后的 `models test` 成功。冷启动后的 Core 不带 SOCKS，LaunchAgent 仍按盘点结论指向包装。停止前的 pid 已经退出，两分钟后新 pid 不变。已有记忆时 `recall` 返回了你预期的原文或记忆。history 没有因启动而回流，sync 只增量前进。

## 按需参考

CLI 的 `upgrade` 在迁移开始前失败时，会把版本文件和 `current` 恢复到 `$PREVIOUS_RUNTIME`，而且当时没有重启到新版本。核对 `readlink` 已经回到 `$PREVIOUS_RUNTIME`。不要再手动改链接，也不要打开 `$APP_PREVIOUS`。只有数据被证明已坏时，才在确认 Runtime 已经停止后做下面的 restore。

App 路径不能套用上面的自动恢复。桌面启动会先安装 Runtime，再做迁移。失败时先核对 `current` 和迁移状态。没有核对之前，不要打开 `$APP_PREVIOUS`，也不要手动改链接。

迁移已经开始。旧 Runtime 不能打开已迁移的数据。不要把 `current` 指回 `$PREVIOUS_RUNTIME`。不要打开 `$APP_PREVIOUS`，打开旧 App 还可能再次进入迁移。新版本没有就绪时，按 CLI 的提示执行 `memhub restart`，再用 `memhub doctor` 看原因。错误码是 `upgrade_health_check_failed` 或 `runtime_not_ready_after_sdk_migration` 时，产品保留新版本。

`backup restore` 只恢复数据，不切换 Runtime。要做时先退出当前 App，执行 `memhub stop`，并确认 Core 进程已经不在，再执行：

```bash
memhub backup restore \
  --source "$DATA_DIR/backups/$BACKUP_NAME.memhub-backup" \
  --yes \
  --confirm "$DATA_DIR/backups/$BACKUP_NAME.memhub-backup"
```

`--confirm` 必须和 `--source` 是同一个最终路径。restore 替换主库和记忆，核对条数，不一致则整份退回。旧的单个 `.sqlite3` 备份不含记忆，不能当作这一对里的数据半边。restore 成功后执行 `memhub start`，仍用当前 `current`，不要换回旧 App。若当前 Runtime 仍不能读回记忆，停在现场，保留 `$PREVIOUS_RUNTIME`、备份目录和日志。不要改 `versions` 里的文件。

清理旧 version 目录、钥匙串和 SOCKS 包装的做法只在踩坑文档里。清理数据目录以外的目录前，先确认例程仍引用的树外脚本还在。见踩坑文档「清理 grok-exp 时勿删例程依赖目录」。那不是本流程的一步。
