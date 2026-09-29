# 本地运行、验证与发布

## 环境与启动

使用 `.python-version` 指定的 Python 3.13、`uv.lock` 锁定的依赖，以及 Node.js 20+ 和 PowerShell 7。首次准备：

```powershell
uv sync --frozen
Push-Location frontend
npm ci
npm run build
Pop-Location
uv run --frozen python -m stock_god db status
```

新安装或有待执行迁移时，先明确目标数据库并备份，然后显式执行 `uv run --frozen python -m stock_god db migrate`。服务启动只检查 schema，待迁移时拒绝就绪。

开发运行：

```powershell
uv run --frozen python -m stock_god serve
```

默认地址为 `http://127.0.0.1:34115`。`serve --no-scheduler` 可用于隔离的页面/API 检查：它恢复中断状态，但不自动启动预测、邮件和行情任务。现有数据库副本路径必须通过环境明确设置；正式运行使用已部署制品及 `runtime/current.json`。

部署后的日常操作：

```powershell
.\启动项目.cmd
pwsh -File scripts/release.ps1 -Command status
pwsh -File scripts/release.ps1 -Command restart
pwsh -File scripts/release.ps1 -Command stop
```

启动器跟随已核验的发布指针。`/livez` 表示进程存活，`/readyz` 同时提供迁移、服务、调度就绪及版本/commit/制品哈希；关于页面展示后端返回的真实版本。

开盘前恢复由当前用户的 Windows 计划任务 `StockGod-0900-EnsureRunning` 承载。首次在本机注册：

```powershell
pwsh -NoProfile -File scripts/ensure-running.ps1 -Mode Install
```

任务每天 09:00 检查一次已部署版本；正常时不重启，停止或已归属但不就绪时安全重启，未知监听进程不会被终止。它仅在当前用户登录或锁屏时运行，允许电池供电，不主动唤醒电脑；睡眠错过时醒来后补执行可能延迟。任务进程保持运行并在下一天 09:00 再检查；日内再次退出要手动处理。结果写入 `runtime/logs/ensure-running.log`。部署完成后应先停止由部署命令启动的服务，再通过 `Start-ScheduledTask -TaskName StockGod-0900-EnsureRunning` 从任务宿主启动正式服务，并核对 `/readyz` 与任务状态。

## 持久数据与临时文件

| 内容 | 默认位置或配置 |
| --- | --- |
| 主数据库 | `data/stock.db`；`STOCK_GOD_DB_PATH` |
| 分钟缓存库 | `data/minute.db`；`STOCK_GOD_MINUTE_DB_PATH` |
| 大型原始行情包 | 项目内 `A股历史分钟线数据包/`；由 `STOCK_GOD_MARKET_DATA_ROOT` 指定 |
| 派生索引 | `runtime/minute-index`；`STOCK_GOD_MARKET_INDEX_DIR` |
| 已部署制品和解释器 | `runtime/releases`、`runtime/toolchain` |
| 部署/回滚记录及数据库备份 | `runtime/deployments` |
| 运行日志 | `runtime/logs` |
| 一次性测试脚本、数据库副本、截图 | `H:\Download` 对应任务目录 |

数据、依赖缓存、日志、构建结果和临时脚本不入 Git。不得把一次性测试指向生产库。只清理本次任务产生的副产物；历史数据、旧版回滚制品及既有用户文件保留。

## 分级验证

```powershell
pwsh -File scripts/verify.ps1 -Tier fast -TestPath tests/prediction/test_prediction.py
pwsh -File scripts/verify.ps1 -Tier domain -Domain prediction
pwsh -File scripts/verify.ps1 -Tier domain -Domain contracts
pwsh -File scripts/verify.ps1 -Tier release
```

领域可选 `prediction`、`market`、`storage`、`web`、`contracts`；前端定向检查使用 `-FrontendTest`，路径相对于 `frontend`。`release` 包括全量离线测试、lint、类型、契约与前端构建，只用于明确的发布或全门禁请求。通过的检查不因无关改动重复执行。

接口变更后运行 `uv run --frozen python -m stock_god.contracts --write` 更新生成 TS，再运行无 `--write` 的检查。边界测试要求已退役路径不再出现、无 Go 运行时源码、包依赖方向和版本一致。

## 6.0.0 发布验收

最终候选必须完成本地 release 门禁、独立冷启动链路、重启/失败恢复链路，以及用户要求的一次真实行情和模型预测。真实预测使用 `H:\Download` 中的临时数据库副本；测试邮件发本地 SMTP fixture。原始输出、临时 test 和数据库副本不入库，保留脱敏验证回执及其哈希。

验证回执绑定 commit、依赖锁和制品身份。构建候选后使用 `scripts/release.py inspect --candidate <目录>` 检查；部署使用：

```powershell
uv run --frozen python scripts/release.py build --uv <uv.exe完整路径>
uv run --frozen python scripts/release.py inspect --candidate <候选目录>
uv run --frozen python scripts/release.py deploy --candidate <候选目录> --proof <验收回执JSON>
```

`proof` 必须来自已执行的验证，不能手写“通过”代替测试。当前四个阶段为 `local-release-gate`、`offline-cold`、`offline-restart`、`live-prediction`。候选内容变化后，旧回执失效。

这四项同时强制适用于 6.0.0 的语言和数据迁移。后续普通版本默认只要求本地 release 门禁；额外完整演练与真实模型调用按用户明确要求执行，日常修复仍使用定向验证。

用户明确授权后才创建 annotated tag `6.0.0` 并推送对应 commit/tag，随后核对远端 SHA。GitHub 使用统一 SSH key 与 `127.0.0.1:7890` 代理，无直连回退，也不创建 GitHub Actions。普通开发 commit 不自动 push。

本机 Codex 的 PowerShell 环境若继承了 `SHELL=...powershell.exe`，OpenSSH 的代理命令会因不支持 `exec` 而失败。执行 GitHub SSH 命令时仅在该进程设置 `$env:SHELL='H:/Program Files (x86)/Git/bin/bash.exe'`，继续使用原 SSH 配置中的账户 key、443 端口和代理。

部署校验候选和回执，停止已识别进程，备份双库，迁移并验证数据库，先验证候选服务，再开启调度；完成后核对 `/readyz`、进程身份和浏览器版本。中断后用 `recover` 恢复未完成的部署；失败时按部署回执恢复双库及旧指针：

```powershell
uv run --frozen python scripts/release.py rollback --receipt <runtime/deployments中的receipt.json>
uv run --frozen python scripts/release.py recover
```

归档的旧 Go 可执行文件只用于已有部署回执的回滚；当前开发、构建和运行均使用 Python。
