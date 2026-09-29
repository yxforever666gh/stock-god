# Stock God Working Rules

These rules apply to the whole repository. Keep ordinary work local and bounded.

## Product and ownership

- The active UI is 股票预测, settings and about. Market-data HTTP APIs and the minute/MCP service remain supported. Research 1, knowledge, stock/fund watchlists and market display pages are retired; do not recreate their entry points.
- Python is the only current backend runtime. Do not restore Go sources, Go wrappers, Wails or a parallel implementation. Archived Go executables are permitted only as existing deployment rollback artifacts outside tracked source.
- `stock_god.prediction` owns strategy, account, execution, returns, reports and task lifecycle. Market/AI providers are injected; prediction must not import concrete `market`, `app`, `cli`, `runtime`, `storage.migrations` or `storage.historical` code.
- `stock_god.market` owns provider I/O and source validation, and must not import prediction or the composition root. `stock_god.storage` must not import current business packages. `config` and `jsonutil` are shared primitives and must not import features.
- Historical migration definitions and algorithms remain frozen in `storage/historical`. Current prediction changes never alter their financial rules.
- Persisted research2 table names, owner values and UUID namespaces are data contracts. Branding changes must not rewrite them.
- Capture a deep settings/model snapshot at every task entry. Provider retries, fallback and evidence collection use that snapshot. Never temporarily replace global settings.
- Saving settings affects later provider work. Disabling automation revokes old run publication/new-buy permission; re-enabling cannot revive it. Existing positions retain their exit handling.

## Keep changes small

- Prefer deletion of obsolete code, simplifying existing code, merging real duplication, then extending an established boundary.
- Optimize correctness, readability and total maintenance cost. Do not compress code merely to reduce physical lines.
- Do not add abstractions, configuration, states or background jobs for hypothetical future use. Extract sharing after three real uses, or when a necessary dependency boundary already exists.
- A routine task should stay in one domain. Split work exceeding ten source files or two domains unless the user explicitly requests a cross-domain change.
- Remove replaced entry points, branches, options and obsolete tests in the same task. A necessary compatibility path must document its exact removal condition.
- Avoid a repository-wide refactor while implementing a small feature. Use the architecture guide to identify the smallest responsible package.
- Explain necessity and long-term cost when adding more than 200 production lines, changing more than ten source files, or adding a public interface, configuration, schema object or background job.

## Verification

- Read-only diagnosis does not run tests by default.
- Routine implementation uses `scripts/verify.ps1 -Tier fast -TestPath <pytest target>` or `-FrontendTest <frontend test>`. Cross-boundary work uses one matching `-Tier domain -Domain prediction|market|storage|web|contracts`.
- API changes update canonical `api/openapi.yaml`, regenerate TS with `python -m stock_god.contracts --write`, and check real FastAPI routes. Never edit generated TS independently as the final state.
- Run affected frontend behavior tests when requests, pages or charts change. `tests/test_boundaries.py` guards package imports, retired surfaces, runtime language and version consistency.
- A requested local version update uses the affected `-Tier domain` checks and frontend behavior tests. A requested new `X.0.0` runs `-Tier major`: all offline tests, contracts and frontend build, without routine lint or type checks. `-Tier release` is only for an explicit full-gate request.
- Ordinary fixes do not automatically build, deploy, bump versions or add tags. A requested version update uses `scripts/update-local.ps1` after the final commit, with the affected domain and frontend test paths.
- Do not repeat a passing check unless relevant code changed or an unresolved risk requires it. After two failures with the same cause, stop rerunning and diagnose.
- Report unrelated/pre-existing failures separately. They do not expand the task silently.
- Target three minutes for fast, eight minutes for a domain check, and 10–20 minutes for a routine fix. Major releases are separate work.

## Data and test safety

- Repository tests use pytest `tmp_path` or disposable fixtures. They never migrate or write runtime/production databases.
- Ordinary tests use the minimum current schema fixture; full historical migrations and SQLite integrity checks belong to migration/release-specific tests.
- Live providers, external email and browser probes require an explicit opt-in. They are excluded from ordinary fast/domain/release gates. Test SMTP uses a local fixture.
- Preserve full source evidence in durable storage and audit. Model prompts use compact snapshots and bounded scoring facts, not entire raw market documents.
- Keep meaningful regression tests in the repository. One-off test harnesses, logs, downloaded artifacts, screenshots and database copies belong under `H:\Download` and are not committed.
- Never delete existing user data or unrelated files while cleaning task output. Do not run Git GC or workspace cleanup as part of routine verification.

## Release and completion

- Ordinary commits remain local. For this project, a request to upgrade to a new `X.0.0` authorizes pushing only its verified commit and annotated tag through the configured SSH identity and proxy. Every other version gets a local annotated tag and no GitHub write. Never create a GitHub Release or Actions workflow.
- Versions come from `src/stock_god/release_manifest.json` and must agree with project metadata and runtime identity. Do not bump versions for routine fixes.
- The 6.0.0 language/data migration was a one-time historical acceptance, not a gate for later versions. Temporary test databases and logs stay outside Git.
- Local updates use a versioned source/frontend snapshot and a reusable dependency-locked Python environment. Pure frontend/version/documentation updates skip database work; backend or dependency changes back up both databases, verify them, and migrate only when the schema changes. The existing scheduled task owns activation; verify process identity, `/readyz`, and the version before tagging. Preserve older artifacts and deployment receipts for rollback.
- Failed checks or activation do not create a tag. Never move or overwrite a published tag. A failed major-version GitHub push leaves the successful local deployment/tag intact and must be reported.
- Only one task writes a checkout. Parallel writers use separate Git worktrees and preserve unrelated local changes.
- Completion requires the requested behavior, targeted checks, boundary contracts and `git diff --check`, with no unrelated changes. Stop after those conditions hold.
- End with a short Complexity change note: production lines added/removed, source files touched, execution paths added/removed, public interfaces/configuration/schema added, and any old path still retained. Count physical lines; report prompts, tests, docs, generated metadata and data assets separately.

Active guidance is limited to README, this file, and `docs/architecture.md`, `docs/operations.md`, `docs/data-apis.md`. Historical notes are references, not runtime instructions.
