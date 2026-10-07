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

### 7.0.0 发布与行情配置

7.0.0 按高等更新完成离线 `major` 检查、版本化快照、双库备份、迁移37和正式计划任务激活；发布不再等待人工开盘验收，不要求开盘回执或密钥文件，也不自动导入下载目录的密钥。

部署后由用户在设置页的 MeoZ API Key 密码框填写并保存密钥。未配置密钥不会阻碍应用 `/readyz` 就绪，但不采集和新买入。保存后后续任务使用独立配置快照；更换或清空密钥会使此前来源状态失效。来源就绪由当日实际采集与冻结结果决定，无需人工认证。

选股仍要求完整候选资格、连续竞价覆盖、最终竞价、数量与金额交叉核验、43项特征以及09:29:59前接收的事实。权限错误、缺失或冲突数据、错过冻结窗口都阻止新买入。分钟、盘口和竞价数量按已实测契约处理，不伪造认证或补发买单。

`python -m stock_god.meoz_validation` 保留为可选隔离诊断工具，不参与正常发布授权。版本、commit、进程、模型、迁移和服务就绪核对通过后才创建annotated tag并原子推送；失败继续沿双库回执回滚。原08:55聊天跟进已暂停，正式Windows启动任务及策略调度保留。

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

09:26起每3秒检查完整候选、43项特征和当日五模型，齐备即冻结输入与现金并发布；缺数据等待至09:29:59，截止仍不齐则不生成买单。成功后当天不重排；信号记录实际冻结时间，模拟买入仍限09:30:00—09:30:59。同一输入与模型快照早晚评分一致，晚到或冲突输入可能改变结果，无需为发布时间重训。
