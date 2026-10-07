# Stock God

Stock God 是 Python、Vue 3 和 SQLite 构建的本地股票预测工具。界面聚焦**股票预测、设置、关于**；完整行情 API、分钟数据与 MCP 独立保留。

[运行与更新](docs/operations.md) · [结构与维护](docs/architecture.md) · [数据接口](docs/data-apis.md) · [发布历史](RELEASE_NOTES.md) · [GitHub](https://github.com/yxforever666gh/stock-god)

> 本项目由公开项目 [ArvinLovegood/go-stock](https://github.com/ArvinLovegood/go-stock) 演化而来，并非原作者官方仓库。原始版权、[LICENSE](LICENSE) 和 [NOTICE](NOTICE) 保留。

## 股票预测

7.0.0 升级目标为唯一 BASE43 模拟账户，初始本金 30,000 元。原 43 特征、最近五模型均值选择正分前两名；盘前现金等分，09:30 首分钟用合格实时报价成交，100 股整手并计入实际费用。

MeoZ 竞价事实通过行情层注入，设置中保存 API Key。未配置、权限不足、尚未完成真实核验或当日资料不完整时不选股、不新买入；已有 BASE43 持仓仍按 T+1 和低开退出规则管理。旧 24 个账户、持仓、报告和收益完整归档，只读查看，不恢复交易、估值或回放写入。

收盘后更新研究参考票标签并训练下一交易日模型，新账户收益使用独立成交账本。历史条件回放累计收益 687.84%、几何日均 0.4880%、最大回撤 24.99%，不代表独立样本外结果或当前实时报价账户收益。7.0.0 仍须通过真实接口与开盘竞价验收，才可部署、打 tag 和推送。

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

本工具使用模拟账户，预测结果供学习研究，不构成投资建议。
