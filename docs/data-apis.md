# 行情、分钟数据与 MCP

股票预测页面保留推荐内图表。市场、股票自选、基金自选和排行榜页面已移除；行情 HTTP API、基金/ETF 数据、题材、指数、资金流、公告和研报接口继续存在。

## 主服务

默认 `http://127.0.0.1:34115`，规范入口为 `/openapi.json`，源码为 [`api/openapi.yaml`](../api/openapi.yaml)。保留行情路径位于 `/api/v1/market`、`/stocks`、`/instruments`、`/themes`、`/funds`、`/etfs`。

预测接口统一为 `/api/v1/prediction`；设置、模型列表、审计及模型回放属于此命名空间。旧研究接口、自选/分组、知识库接口不提供别名。

分析报告浏览使用 `GET /api/v1/prediction/analysis-runs/browse`，按最近五个有报告的交易日、指定交易日或全部日期返回每页 100 条摘要、总数与可选交易日；原列表接口保留兼容。

股票 snapshot 可以独立于自选记录取行情；存在旧自选时只读历史成本、数量等兼容字段，读取不更新历史持仓。行情缺失不使用伪造零值或预测账户仓位填补。

## 分钟与 MCP 服务

```powershell
uv run --frozen python -m stock_god.market.minute_app --data-root <原始行情包目录> --prepare-data
uv run --frozen python -m stock_god.market.minute_app --data-root <原始行情包目录>
```

默认监听 `127.0.0.1:18080`。索引保存位置由 `--index-dir` 或 `STOCK_GOD_MARKET_INDEX_DIR` 指定，原 CSV 保留原地。个股、指数和竞价数据只在存在可核验记录时返回；空缺、损坏、冲突和来源不足明确报告。

主要 HTTP 路径：`/livez`、`/readyz`、`/api/bars`、`/api/indices`、`/api/index/bars`、`/api/auction/snapshots`。MCP 入口为 `/mcp`，保留 10 项本地/统一工具（含日线）与 44 项蝶梦接口，共 54 项；请求参数及字段以 MCP `tools/list` 和 [`mcp_catalog.py`](../src/stock_god/market/mcp_catalog.py) 为准。

统一分钟工具的 `source` 可选 `auto`、`local`、`diemeng`。本地已有部分记录时不拼造缺失分钟；显式 `diemeng` 使用远端区间。CSV 复权属性为 `unknown`，有来源证明的远端历史分钟为 `none`。不把复权数据当原始执行价。

## 私人来源与密钥

股票预测设置中保存私人分钟来源 URL、Key、超时、最小间隔及来源顺序。一次任务捕获配置快照，保存配置只影响后续任务。预测核心取数链与图表来源配置有独立边界，见设置页面说明。

独立分钟服务使用本机私密 JSON 文件，可通过 `--diemeng-config <文件>` 指定。内容包含 `base_url` 和 `api_key`；文件置于源码仓库外，不上传日志、截图或 Git。44 项原接口的详细字段保留于 [接口参考](reference/mcp-catalog.md)。

## Cloudflare 临时连接

```powershell
uv run --frozen python -m stock_god.market.tunnel --data-root <原始行情包目录> --cloudflared <cloudflared.exe完整路径>
uv run --frozen python -m stock_god.market.tunnel --stop
```

隧道流量通过本机 `127.0.0.1:7890`，代理不可用时停止。启动器拥有其 API、转发和隧道子进程；索引及就绪检查完成后将当前完整地址写入 `runtime/minute-api/mcp-url.txt`。

Quick Tunnel 重启后地址可能变化。连接端应重新填写本次完整 `/mcp` 地址并扫描工具；入口使用 None/无认证，知道地址的人可调用服务。数据下载工具有明确写文件标记，保存位置在运行数据目录，不能写入源码。

旧验证记录在 `docs/history`；它们描述当时的数据和测试，不替代当前发布验证。
