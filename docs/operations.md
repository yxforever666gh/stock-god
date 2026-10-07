# 本地运行、验证与更新

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
pwsh -File scripts/verify.ps1 -Tier major
pwsh -File scripts/verify.ps1 -Tier release
```

领域可选 `prediction`、`market`、`storage`、`web`、`contracts`；前端定向检查使用 `-FrontendTest`，路径相对于 `frontend`。`major` 跑全部离线 pytest、契约、前端行为测试和构建；`release` 额外包含 lint 和类型检查，只用于明确要求全门禁时。通过的检查不因无关改动重复执行。

接口变更后运行 `uv run --frozen python -m stock_god.contracts --write` 更新生成 TS，再运行无 `--write` 的检查。边界测试要求已退役路径不再出现、无 Go 运行时源码、包依赖方向和版本一致。

## 本地版本更新

### 7.0.0 专项发布门禁

BASE43 与 MeoZ 的离线实现不等同于已发布。先受控验证账号权限、字段与节点；再于真实交易日 09:15 前启动隔离采集，核验完整候选母体、09:20、09:24、09:24:50 检查点、最终竞价、数量单位、覆盖间隔及 09:29:59 前接收时间。首次实测仅验证，不写模拟买单。密钥只在进程内从文件读取，不进入请求文件、命令参数值、日志或 Git。

```powershell
.venv/Scripts/python.exe -m stock_god.meoz_validation --date <已核交易日> --key-file <密钥文件> --output H:/Download/stock-god-base43-acceptance
```

命令生成独立 UUID 目录、原始事实库及 `receipt.json`。成功后用 `--accept <该UUID目录> --key-file <密钥文件>` 只读复核；从干净提交执行 `pwsh -File scripts/update-local.ps1 -MeozAcceptance <该UUID目录> -MeozKeyFile <密钥文件>`。脚本在部署前重验原始事实，迁移 37 后、计划任务激活前保存密钥及同账号认证；失败沿既有双库回滚流程处理。

无密钥、无权限或未完成开盘验收时停在待验收状态，不部署、不创建 7.0.0 tag、不推送。来源未配置本身不使整个服务拒绝就绪，但必须显示业务阻断状态，不能将 `/readyz` 成功当作实时选股验证。通过后才运行 `major`、版本同步、干净发布 checkout、快照、双库备份、迁移及计划任务激活；核对版本、commit、进程、模型及来源身份后创建 annotated tag 并原子推送 main 和 tag。

旧 24 个账户归档后不再交易、估值或恢复写入；BASE43 初始本金 30,000 元只入账一次，重启不重复成交。收盘后滚动训练失败保持失败状态，次日不能冒用已更新标记；错过盘前冻结不补发买单。旧数据与回滚制品不得清理。

代码和版本号提交后，从干净的 checkout 执行一次更新命令：

```powershell
pwsh -File scripts/update-local.ps1 -Domain prediction
pwsh -File scripts/update-local.ps1 -Domain web -FrontendTest src/components/prediction-pages.test.mjs
```

`-Domain` 可传多个受影响领域；改了前端必须指定相应 `-FrontendTest`。新 `X.0.0` 自动改用 `major` 验证，无需传领域。命令检查版本一致和已有 tag，运行测试，保存日志到 `H:\Download\stock-god-update`，只构建一次前端，并自动生成绑定 commit、锁文件及快照哈希的验证记录。失败时不打 tag、不部署。

快照位于 `runtime/releases/<版本>/<commit>`，包含源码、API 和前端文件；相同依赖锁和解释器复用 `runtime/toolchain/envs` 内的 Python 环境。前端未改动时复用上一版产物。旧格式制品继续可用于现有部署回执的回退。

部署对照当前运行 commit：纯前端、文档、测试和版本号改动不碰数据库；其余改动停机后备份双库并校验，只有 schema 变化才迁移。现有 `StockGod-0900-EnsureRunning` 计划任务发现待激活回执后启动候选；命令确认 `/readyz`、版本、commit 和进程身份才创建本地 annotated tag。每阶段耗时会输出到终端。更新前必须安装该计划任务。

只有用户要求升级到新的 `X.0.0` 时，成功本地部署并打 tag 后才通过配置好的 SSH 代理原子推送 `main` 和 tag，再核对远端 SHA。其他版本不写 GitHub；不创建 GitHub Release 或 Actions。普通开发 commit 不自动部署或 push。

本机 Codex 的 PowerShell 环境若继承了 `SHELL=...powershell.exe`，OpenSSH 的代理命令会因不支持 `exec` 而失败；更新命令仅在 GitHub 子步骤为该进程改设 Git Bash，继续使用账户 key、443 端口和 `127.0.0.1:7890` 代理，无直连回退。

激活失败时回到旧版本；有双库备份时按回执恢复数据库，纯前端更新只恢复旧指针。中断后用 `recover` 检查待处理回执；需要手动回退时：

```powershell
pwsh -File scripts/release.ps1 -Command recover
pwsh -File scripts/release.ps1 -Command rollback --receipt <runtime/deployments中的receipt.json>
```

6.0.0 的双离线链路与真实行情/模型预测是已完成的一次性迁移验收，不适用于后续普通更新。归档的旧 Go 可执行文件只用于已有部署回执的回滚；当前开发、构建和运行均使用 Python。
