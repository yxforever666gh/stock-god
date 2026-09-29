# Stock God

Stock God 是 Python、Vue 3 和 SQLite 构建的本地股票预测工具。界面聚焦**股票预测、设置、关于**；完整行情 API、分钟数据与 MCP 独立保留。

[运行与更新](docs/operations.md) · [结构与维护](docs/architecture.md) · [数据接口](docs/data-apis.md) · [发布历史](RELEASE_NOTES.md) · [GitHub](https://github.com/yxforever666gh/stock-god)

> 本项目由公开项目 [ArvinLovegood/go-stock](https://github.com/ArvinLovegood/go-stock) 演化而来，并非原作者官方仓库。原始版权、[LICENSE](LICENSE) 和 [NOTICE](NOTICE) 保留。

## 股票预测

交易日 09:30 至 11:25 每五分钟一个独立账户，共 24 个。分析依据冻结行情和可核验来源评分，报告按实际启动时间归区间，同区间最先完成的合格报告进入模拟买入；11:30 后完成的报告保留归档。

每个账户每天最多五笔买入，按当前剩余现金除以剩余名额分配，含费不透支。旧持仓在对应时段独立退出，模型失败或关闭自动策略不取消已有持仓管理。页面提供推荐、报告、净收益、图表和不可变证据审计；模型对照回放不改正式账户。

当前数据仍使用既有 `research2_*` 身份。研究中心一、知识库、自选等退役功能的数据保留在同一数据库及归档中。历史迁移与当前预测规则保持独立。

## 本地启动

准备 `.python-version` 指定的 Python 3.13、uv、Node.js 20+、PowerShell 7：

```powershell
uv sync --frozen
Push-Location frontend
npm ci
npm run build
Pop-Location
uv run --frozen python -m stock_god db status
```

在明确备份和目标库后，用 `uv run --frozen python -m stock_god db migrate` 执行待处理迁移，再启动：

```powershell
uv run --frozen python -m stock_god serve
```

打开 `http://127.0.0.1:34115`。已部署版本使用 `启动项目.cmd`，它跟随已核验的发布指针；开发运行和正式制品不要同时占用端口。

主服务规范为 `/openapi.json`；独立分钟/MCP 服务默认使用 `127.0.0.1:18080`。大型行情包可通过 `STOCK_GOD_MARKET_DATA_ROOT` 放在仓库外。数据库、原始行情、运行日志和临时测试不入 Git。

## 修改与验证

日常改动先定位所属包，再运行定向验证，例如：

```powershell
pwsh -File scripts/verify.ps1 -Tier fast -TestPath tests/prediction/test_prediction.py
pwsh -File scripts/verify.ps1 -Tier domain -Domain contracts
```

约束见 [AGENTS.md](AGENTS.md)。普通开发不自动升版本或部署；请求本地版本更新时使用 `scripts/update-local.ps1` 完成领域验证、快照、受控激活和本地 tag。只有新的 `X.0.0` 会推送对应提交与 tag 到 GitHub。

本工具使用模拟账户，AI 输出供学习研究，不构成投资建议。
