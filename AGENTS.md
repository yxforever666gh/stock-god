# Stock God 工作规则

本文件只保留代理必须执行的约束；架构、运行和接口细节分别见 [架构](docs/architecture.md)、[运维](docs/operations.md) 和 [数据接口](docs/data-apis.md)。

## 产品与边界

- 现役界面只有股票预测、设置和关于；退役的研究中心、知识库、自选和市场展示入口不恢复。
- Python 是唯一现役后端运行时；不得恢复 Go、Wails 或并行实现。
- `prediction` 负责策略、账户、执行、收益、报告和任务生命周期；行情与 AI 通过注入使用。
- `prediction` 不得导入具体 `market`、`app`、`cli`、`runtime`、`storage.migrations` 或 `storage.historical`；`market` 不得反向导入 `prediction`；`storage` 不得导入业务包。
- `config` 和 `jsonutil` 只能作为共享基础模块，不得导入功能模块。
- `research2_*` 表名、owner、UUID 命名空间和 `storage/historical` 历史规则是数据契约，当前改动不得重写。
- 每个任务入口深拷贝设置和模型快照；提供者重试、回退和证据采集不得临时改写全局设置。

## 改动与数据安全

- 优先删除过时代码、合并真实重复，保持改动范围小；新增公共接口、配置、schema、后台任务，或生产代码超过 200 行时说明必要性和维护成本。
- 测试只能使用 `tmp_path` 或一次性数据库，绝不写运行时或生产库；测试、日志、下载物、数据库副本和截图放在 `H:\Download`。
- 不删除既有用户数据、回滚制品或无关文件；完成前清理本次产生且已确认可重建的副产物。

## 验证

- 只读诊断默认不运行测试；实现使用 `scripts/verify.ps1` 的 `fast`、`domain`、`major` 或 `release` 检查，具体入口和领域说明见 [运维文档](docs/operations.md)。
- API 改动先改 `api/openapi.yaml`，运行 `python -m stock_god.contracts --write`，再检查真实路由；不得只改生成的 TypeScript。
- 请求、页面或图表改动运行受影响的前端行为测试；跨包改动运行对应领域检查，边界检查必须通过。
- 已通过的检查不因无关改动重复执行；同一原因连续失败两次后停止重跑并诊断。完成条件包括请求行为、定向检查、边界契约、`git diff --check` 和无无关改动。

## 版本与发布

- 版本级别：高等更新为 `X.0.0`，中等更新为 `X.Y.0`，低等更新为 `X.Y.Z`；版本来源是 `src/stock_god/release_manifest.json`，并须与项目元数据和锁文件一致。
- 纯分钟数据包、原始行情包或其索引变化不单独触发版本升级或 tag；若同一提交含生产代码，按生产代码级别处理。
- 普通提交不 push、不创建 Release 或 Actions；版本升级须先完成领域验证、快照、部署、计划任务重启和 `/readyz`、版本、commit 核对，再创建本地 annotated tag。
- 只有高等 `X.0.0` 更新按项目既有 SSH 规则推送 GitHub；不得移动、覆盖、删除或 force-push 已发布 tag。激活或核对失败时不创建 tag。

## 复杂度说明

结尾简述生产代码新增/删除行数、源码文件数、执行路径变化、公共接口/配置/schema 变化，以及保留的旧路径；提示词、测试、文档、生成元数据和数据资产分别统计。
