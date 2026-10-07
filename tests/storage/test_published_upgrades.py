import base64
import json
from pathlib import Path

import pytest

from stock_god.storage.db import Database, quote_identifier as qi
from stock_god.storage.migrations import migrate, PUBLISHED_VERSIONS


DATA = json.loads(
    (Path(__file__).parents[1] / "fixtures/migrations/published_databases.json").read_text(encoding="utf8")
)
CASES = DATA["fixtures"]


def test_every_published_tag_has_an_original_schema_fixture():
    assert {v["tag"] for v in PUBLISHED_VERSIONS} == {tag for case in CASES for tag in case["tags"]}


def load_published(db, case):
    objects = [DATA["objects"][key] for key in case["objects"]]
    for obj in sorted(objects, key=lambda o: {"table": 0, "index": 1, "trigger": 2}[o["type"]]):
        if obj["type"] == "trigger":
            continue
        db.execute(obj["sql"])
    for table, items in case["tables"].items():
        for item in items:
            values = [
                base64.b64decode(v["$blob"]) if isinstance(v, dict) and "$blob" in v else v
                for v in item.values()
            ]
            db.execute(
                "INSERT INTO "
                + qi(table)
                + " ("
                + ",".join(qi(c) for c in item)
                + ") VALUES ("
                + ",".join("?" for _ in item)
                + ")",
                values,
            )
    for obj in objects:
        if obj["type"] == "trigger":
            db.execute(obj["sql"])


@pytest.mark.migration
@pytest.mark.parametrize("case", CASES, ids=lambda c: c["tags"][-1])
def test_published_database_upgrades_directly_with_python(case, tmp_path):
    main = tmp_path / "main.db"
    with Database(main).transaction() as db:
        load_published(db, case)
    result = migrate(main, tmp_path / "minute.db")
    assert result["main"]["currentVersion"] == 37
