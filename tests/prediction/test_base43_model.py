import json

import numpy as np
import pytest

from stock_god.prediction.base43 import ASSETS, MANIFEST, Base43Models, feature_candidate, reference_label
from stock_god.prediction.base43_auction import summarize_group
from stock_god.storage.db import Database


@pytest.fixture
def models(tmp_path):
    db = Database(tmp_path / "models.db")
    with db.transaction() as con:
        con.execute(
            "CREATE TABLE research2_base43_samples(sample_id TEXT PRIMARY KEY,trade_date TEXT,code TEXT,feature_json TEXT,roi REAL,mature_date TEXT,label_status TEXT,evidence_json TEXT)"
        )
        con.execute(
            "CREATE TABLE research2_base43_models(prediction_date TEXT PRIMARY KEY,trained_at TEXT,sample_digest TEXT,model_blob BLOB,metadata_json TEXT)"
        )
    result = Base43Models(db)
    result.bootstrap()
    yield result
    db.close()


def test_frozen_five_models_and_seed(models):
    assert models.health()["ready"]
    assert models.snapshot().dates == ("20260923", "20260924", "20260928", "20260929", "20260930")
    with np.load(ASSETS / "seed.npz") as seed:
        x = seed["features"][-10:]
    snapshot = models.snapshot()
    expected = np.stack([m.predict(x) for m in snapshot.models]).mean(axis=0)
    np.testing.assert_array_equal(models.predict(x, snapshot), expected)
    models.bootstrap()
    with models.database.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM research2_base43_samples").fetchone()[0] == MANIFEST["rows"]


def test_daily_fit_maturity_and_idempotence(models):
    models.record_candidate("20261001", "sz000001", np.full(43, np.nan), {})
    models.mature("20261001:sz000001", 99, "20261008", {"source": "future_label"})
    before = models.prepare("20261008")
    again = models.prepare("2026-10-08")
    assert before.hashes == again.hashes
    with models.database.connection() as con:
        receipt = json.loads(
            con.execute(
                "SELECT metadata_json FROM research2_base43_models WHERE prediction_date='20261008'"
            ).fetchone()[0]
        )
    assert receipt["latest_training_maturity_date"] < 20261008
    assert receipt["parameters"] == MANIFEST["parameters"]
    assert receipt["training_dates"] >= 120


def test_unknown_labels_and_frozen_features(models):
    models.record_candidate("20261001", "sz000001", np.full(43, np.nan), {})
    models.mature("20261001:sz000001", None, "20261008", {}, "unknown")
    with models.database.connection() as con:
        row = con.execute(
            "SELECT roi,label_status FROM research2_base43_samples WHERE sample_id='20261001:sz000001'"
        ).fetchone()
    assert tuple(row) == (None, "unknown")
    with pytest.raises(ValueError, match="frozen"):
        models.record_candidate("20261001", "sz000001", np.zeros(43), {})
    with pytest.raises(ValueError, match="finite ROI"):
        models.mature("20261001:sz000001", None, "20261008", {})


def test_health_rejects_corrupted_weight(models):
    with models.database.transaction() as con:
        con.execute(
            "UPDATE research2_base43_models SET model_blob=? WHERE prediction_date='20260930'", (b"corrupt",)
        )
    assert not models.health()["ready"]
    assert "weight_identity_mismatch:20260930" in models.health()["errors"]
    with pytest.raises(ValueError, match="identity mismatch"):
        models.snapshot()


def test_mature_pending_keeps_missing_reference_unknown(models):
    models.record_candidate("20261001", "sz000001", np.full(43, np.nan), {})

    class Inputs:
        records = {}

    result = models.mature_pending(Inputs(), "20261001", "20261008", {}, None, None)
    assert result == {"resolved": 0, "unknown": 1, "pending": False}
    with models.database.connection() as con:
        row = con.execute(
            "SELECT roi,label_status FROM research2_base43_samples WHERE sample_id='20261001:sz000001'"
        ).fetchone()
    assert tuple(row) == (None, "unknown")


def test_maturity_preserves_frozen_unknown_and_original_candidate(models):
    day = "20260930"
    with models.database.connection() as con:
        seed = con.execute(
            "SELECT sample_id,roi,mature_date,label_status,evidence_json FROM research2_base43_samples WHERE trade_date=? AND label_status='unknown' LIMIT 1",
            (day,),
        ).fetchone()
    assert seed is not None
    candidate = {"code": "sz999999", "reference": 1000, "sourceSnapshotOriginal": "frozen-live"}
    models.record_candidate(day, candidate["code"], np.full(43, np.nan), candidate)

    class Inputs:
        records = {}

    assert models.mature_pending(Inputs(), day, "20261008", {}, None, None)["unknown"] == 1
    models.mature_pending(Inputs(), day, "20261008", {}, None, None)
    with models.database.connection() as con:
        preserved = con.execute(
            "SELECT sample_id,roi,mature_date,label_status,evidence_json FROM research2_base43_samples WHERE sample_id=?",
            (seed[0],),
        ).fetchone()
        evidence = json.loads(
            con.execute(
                "SELECT evidence_json FROM research2_base43_samples WHERE sample_id=?", (day + ":sz999999",)
            ).fetchone()[0]
        )
    assert tuple(preserved) == tuple(seed)
    assert {k: evidence[k] for k in candidate} == candidate
    assert evidence["labelEvidence"]["exit_status"] == "unknown_entry_qualification"
    models.mature(seed[0], 9, "20261008", {})
    with models.database.connection() as con:
        assert (
            con.execute(
                "SELECT label_status FROM research2_base43_samples WHERE sample_id=?", (seed[0],)
            ).fetchone()[0]
            == "unknown"
        )


def test_auction_conflict_preserves_missing_and_qualification():
    values = np.full((2, 17), np.nan)
    values[:, :4] = [[10, 10, 100, 1000], [10, 10.1, 100, 1010]]
    features, evidence = summarize_group(np.array([33900, 33900]), values)
    assert evidence[7] == 1 and np.isnan(features).all()
    assert feature_candidate({"qualificationKnown": False}) is None


def test_reference_unknown_action_and_first_unknown_exit():
    class Inputs:
        records = {
            (20261001, "sz000001"): {"known": True, "eligible": True, "lower": 900, "upper": 1100},
            (20261008, "sz000001"): {"known": True, "reference": 1001, "lower": 900, "upper": 1100},
        }

        def minute_arrays(self, symbol, day):
            return np.full((241, 4), 1000, dtype=int), np.full(241, 1000000.0)

    def buy(price, qty, **kwargs):
        return (price * qty + 500,)

    def sell(price, qty, **kwargs):
        return (price * qty - 500,)

    inputs = Inputs()
    row = reference_label(inputs, "sz000001", 20261001, 20261008, {}, buy, sell)
    assert row["exit_status"] == "unknown_action_or_reference" and np.isnan(row["net_pct"])
    inputs.records[(20261008, "sz000001")]["reference"] = 1000
    row = reference_label(inputs, "sz000001", 20261001, 20261008, {}, buy, sell)
    assert row["sold"] and row["sell_bar"] == 1


def test_0926_and_092955_identical_frozen_inputs_have_identical_vectors_and_five_model_scores(models):
    from copy import deepcopy
    c = {"qualificationKnown": True, "eligible": True, "priorStreak": 1,
         "reference": 900, "lower": 810, "upper": 1100,
         "historicalAuctionVolumes": [100] * 20, "previousBars": [],
         "auctionRows": [{"time": 33900, "receivedAt": "2026-09-30T09:25:01+08:00",
                          "fields": [9, 10, 100, 1000] + [None] * 13}]}
    early = feature_candidate(deepcopy(c))
    late = feature_candidate(deepcopy(c))
    np.testing.assert_array_equal(early, late)
    snapshot = models.snapshot("20260930")
    np.testing.assert_array_equal(models.predict([early], snapshot), models.predict([late], snapshot))
    changed = deepcopy(c)
    changed["auctionRows"].append({"time": 33900, "receivedAt": "2026-09-30T09:27:00+08:00",
                                  "fields": [9, 11, 100, 1100] + [None] * 13})
    assert feature_candidate(changed) is None
