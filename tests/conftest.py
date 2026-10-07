"""All ordinary tests use disposable resources; live probes require explicit opt-in."""

import json
from pathlib import Path

import pytest

from stock_god.storage.db import Database


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False, help="Enable external provider probes")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="external probes require --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture
def core_database(tmp_path):
    database = Database(tmp_path / "core.db")
    schema = Path(__file__).parent / "fixtures" / "core_schema.sql"
    with database.connection() as connection:
        connection.executescript(schema.read_text(encoding="utf-8"))
    yield database
    database.close()


@pytest.fixture
def app_config(tmp_path):
    """Create the frozen current Go schema directly, without historical migrations."""
    from stock_god.config import AppConfig
    from stock_god.storage.archive import create_archive_schema
    from stock_god.storage.current import apply_base43_schema
    from stock_god.storage.migrations import LEDGER_SQL, MANIFESTS, _expected

    for kind in ("main", "minute"):
        path = tmp_path / "data" / ("stock.db" if kind == "main" else "minute.db")
        objects, _ = _expected(kind, 35 if kind == "main" else 3)
        with Database(path).transaction() as connection:
            for obj in sorted(objects.values(), key=lambda obj: obj["type"] != "table"):
                if not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name=?", (obj["name"],)
                ).fetchone():
                    connection.execute(obj["sql"])
            connection.execute(LEDGER_SQL)
            for step in MANIFESTS[kind]:
                connection.execute(
                    "INSERT INTO schema_migrations VALUES(?,?,?,?,?)",
                    (step["id"], step["name"], step["checksum"], "2026-09-24T00:00:00Z", "6.0.0"),
                )
            if kind == "main":
                create_archive_schema(connection)
                golden = json.loads(
                    (Path(__file__).parent / "fixtures/migrations/main_empty_golden.json").read_text(
                        encoding="utf-8"
                    )
                )
                for table in (
                    "research_settings",
                    "research2_accounts",
                    "research2_account_capital_events",
                    "research2_account_ledger_snapshots",
                ):
                    for row in golden[table]:
                        connection.execute(
                            f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                            tuple(row.values()),
                        )
                apply_base43_schema(connection, "2026-09-24T09:00:00+08:00")
                connection.execute(
                    "INSERT INTO settings(id,dark_theme,refresh_interval,update_basic_info_on_start,enable_news) VALUES(1,0,60,0,0)"
                )
    frontend = tmp_path / "frontend" / "dist"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text("<!doctype html><title>Stock God</title>", encoding="utf-8")
    return AppConfig(
        tmp_path,
        tmp_path / "data/stock.db",
        tmp_path / "data/minute.db",
        frontend,
        tmp_path / "minute-data",
        tmp_path / "runtime/minute-index",
        scheduler_enabled=False,
    )
