"""Frozen BASE43 architecture, independent reference targets and atomic daily fits."""

from __future__ import annotations

import hashlib
import io
import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor
from threadpoolctl import threadpool_limits

from .base43_auction import summarize_group
from .base43_features import build_previous_features

ASSETS = Path(__file__).parent / "assets" / "base43"
MANIFEST = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
FEATURE_NAMES = tuple(MANIFEST["feature_names"])
MODEL_PARAMS = MANIFEST["parameters"]


def _date(value):
    return str(value).replace("-", "")[:8]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


@dataclass(frozen=True)
class Base43Snapshot:
    models: tuple
    dates: tuple[str, ...]
    hashes: tuple[str, ...]

    @property
    def identity(self):
        return hashlib.sha256(_json(self.hashes).encode()).hexdigest()


class Base43Models:
    def __init__(self, database):
        self.database = database
        self._lock = threading.RLock()

    def _runtime(self):
        if sklearn.__version__ != "1.7.2":
            raise RuntimeError("BASE43 requires sklearn 1.7.2")

    def bootstrap(self):
        self._runtime()
        seed_path = ASSETS / "seed.npz"
        if hashlib.sha256(seed_path.read_bytes()).hexdigest() != MANIFEST["seed_sha256"]:
            raise ValueError("BASE43 seed identity mismatch")
        with self._lock, self.database.transaction() as con:
            for item in MANIFEST["models"]:
                blob = (ASSETS / item["file"]).read_bytes()
                if hashlib.sha256(blob).hexdigest() != item["sha256"]:
                    raise ValueError("BASE43 source weight identity mismatch")
                con.execute(
                    "INSERT OR IGNORE INTO research2_base43_models VALUES (?,?,?,?,?)",
                    (str(item["date"]), "research_seed", "research_seed", blob, _json(item["metadata"])),
                )
            with np.load(seed_path, allow_pickle=False) as archive:
                seed = {key: archive[key] for key in archive.files}
                rows = []
                for i, code in enumerate(seed["codes"]):
                    day = str(int(seed["dates"][i]))
                    known = bool(seed["known"][i])
                    x = [float(v) if np.isfinite(v) else None for v in seed["features"][i]]
                    rows.append(
                        (
                            day + ":" + str(code),
                            day,
                            str(code),
                            _json(x),
                            float(seed["roi"][i]) if known else None,
                            str(int(seed["mature_dates"][i])),
                            "known" if known else "unknown",
                            _json({"source": "frozen_raw_roi", "training_facts": 11}),
                        )
                    )
                con.executemany(
                    "INSERT OR IGNORE INTO research2_base43_samples VALUES (?,?,?,?,?,?,?,?)", rows
                )
        return self.health()

    def health(self):
        with self.database.connection() as con:
            rows = con.execute(
                "SELECT prediction_date,model_blob,metadata_json FROM research2_base43_models ORDER BY prediction_date DESC LIMIT 5"
            ).fetchall()
        errors = []
        if len(FEATURE_NAMES) != 43 or len(set(FEATURE_NAMES)) != 43:
            errors.append("feature_contract_invalid")
        seed = ASSETS / "seed.npz"
        if not seed.is_file() or hashlib.sha256(seed.read_bytes()).hexdigest() != MANIFEST["seed_sha256"]:
            errors.append("seed_identity_mismatch")
        source = {str(item["date"]): item for item in MANIFEST["models"]}
        for row in rows:
            try:
                metadata = json.loads(row[2])
                expected = metadata.get("model_sha256") or source.get(row[0], {}).get("sha256")
                if not expected or hashlib.sha256(row[1]).hexdigest() != expected:
                    errors.append("weight_identity_mismatch:" + row[0])
                if (
                    tuple(metadata["feature_names"]) != FEATURE_NAMES
                    or metadata["parameters"] != MODEL_PARAMS
                ):
                    errors.append("architecture_mismatch:" + row[0])
                if int(metadata["latest_training_maturity_date"]) >= int(row[0]):
                    errors.append("future_training_label:" + row[0])
            except (KeyError, ValueError, TypeError):
                errors.append("invalid_weight_metadata:" + row[0])
        return {
            "ready": len(rows) == 5 and sklearn.__version__ == "1.7.2" and not errors,
            "sklearnVersion": sklearn.__version__,
            "errors": errors,
            "featureCount": 43,
            "modelDates": [r[0] for r in reversed(rows)],
            "sourceSeedSha256": MANIFEST["seed_sha256"],
        }

    def snapshot(self, day=None):
        self._runtime()
        with self.database.connection() as con:
            rows = con.execute(
                "SELECT prediction_date,model_blob,metadata_json FROM research2_base43_models WHERE prediction_date<=? ORDER BY prediction_date DESC LIMIT 5",
                (_date(day) if day else "99999999",),
            ).fetchall()
        if len(rows) != 5:
            raise RuntimeError("BASE43 five-weight window unavailable")
        models, dates, hashes = [], [], []
        for row in reversed(rows):
            metadata = json.loads(row[2])
            expected = metadata.get("model_sha256") or next(
                (item["sha256"] for item in MANIFEST["models"] if str(item["date"]) == row[0]), None
            )
            if not expected or hashlib.sha256(row[1]).hexdigest() != expected:
                raise ValueError("BASE43 weight identity mismatch")
            value = joblib.load(io.BytesIO(row[1]))
            if tuple(value["feature_names"]) != FEATURE_NAMES or value["parameters"] != MODEL_PARAMS:
                raise ValueError("BASE43 architecture mismatch")
            if int(value["latest_training_maturity_date"]) >= int(row[0]):
                raise ValueError("BASE43 future training label")
            models.append(value["model"])
            dates.append(row[0])
            hashes.append(hashlib.sha256(row[1]).hexdigest())
        return Base43Snapshot(tuple(models), tuple(dates), tuple(hashes))

    def prepare(self, day):
        self._runtime()
        day = _date(day)
        with self._lock:
            with self.database.connection() as con:
                if con.execute(
                    "SELECT 1 FROM research2_base43_models WHERE prediction_date=?", (day,)
                ).fetchone():
                    return self.snapshot(day)
                rows = con.execute(
                    "SELECT sample_id,trade_date,feature_json,roi,mature_date FROM research2_base43_samples WHERE label_status='known' AND roi IS NOT NULL AND mature_date<? AND trade_date<? ORDER BY trade_date,code",
                    (day, day),
                ).fetchall()
            dates = np.array([r[1] for r in rows])
            unique, inverse = np.unique(dates, return_inverse=True)
            if len(unique) < 120:
                raise RuntimeError("BASE43 insufficient mature training dates")
            x = np.array([json.loads(r[2]) for r in rows], dtype=np.float32)
            y = np.array([r[3] for r in rows], dtype=float)
            if not np.isfinite(y).all():
                raise ValueError("BASE43 nonfinite known label")
            weights = 1 / np.bincount(inverse)[inverse]
            weights *= len(weights) / weights.sum()
            digest = hashlib.sha256(_json([list(r) for r in rows]).encode()).hexdigest()
            with threadpool_limits(limits=3):
                model = HistGradientBoostingRegressor(**MODEL_PARAMS).fit(
                    x, np.clip(y, -15, 15), sample_weight=weights
                )
            metadata = {
                "prediction_date": int(day),
                "latest_training_maturity_date": int(max(r[4] for r in rows)),
                "feature_names": list(FEATURE_NAMES),
                "parameters": MODEL_PARAMS,
                "training_rows": len(rows),
                "training_dates": len(unique),
                "sample_digest": digest,
                "date_weighting": "equal total weight per entry date",
                "label_clip_pct": [-15, 15],
            }
            stream = io.BytesIO()
            joblib.dump(dict(metadata, model=model), stream, compress=3)
            metadata["model_sha256"] = hashlib.sha256(stream.getvalue()).hexdigest()
            with self.database.transaction() as con:
                con.execute(
                    "INSERT OR IGNORE INTO research2_base43_models VALUES (?,?,?,?,?)",
                    (day, datetime.now().isoformat(), digest, stream.getvalue(), _json(metadata)),
                )
            return self.snapshot(day)

    def predict(self, x, snapshot=None):
        x = np.asarray(x, dtype=np.float32)
        if x.ndim != 2 or x.shape[1] != 43 or np.isinf(x).any():
            raise ValueError("BASE43 needs 43 ordered finite-or-NaN features")
        snapshot = snapshot or self.snapshot()
        if not len(x):
            return np.empty(0)
        with threadpool_limits(limits=3):
            scores = np.stack([m.predict(x) for m in snapshot.models]).mean(axis=0)
        if not np.isfinite(scores).all():
            raise ValueError("BASE43 invalid prediction")
        return scores

    def record_candidate(self, day, code, x, evidence):
        x = np.asarray(x, np.float32)
        if x.shape != (43,) or np.isinf(x).any():
            raise ValueError("BASE43 candidate feature shape")
        day = _date(day)
        features = _json([float(v) if np.isfinite(v) else None for v in x])
        with self.database.transaction() as con:
            old = con.execute(
                "SELECT feature_json FROM research2_base43_samples WHERE sample_id=?", (day + ":" + code,)
            ).fetchone()
            if old and old[0] != features:
                raise ValueError("BASE43 frozen candidate changed")
            con.execute(
                "INSERT OR IGNORE INTO research2_base43_samples VALUES (?,?,?,?,?,?,?,?)",
                (day + ":" + code, day, code, features, None, None, "pending", _json(evidence)),
            )

    def mature(self, sample_id, roi, mature_date, evidence, status="known"):
        if status not in ("known", "unknown", "pending"):
            raise ValueError("BASE43 label status")
        if status == "known" and (roi is None or not np.isfinite(roi)):
            raise ValueError("BASE43 known label requires finite ROI")
        with self.database.transaction() as con:
            previous = con.execute(
                "SELECT evidence_json,label_status FROM research2_base43_samples WHERE sample_id=?",
                (sample_id,),
            ).fetchone()
            if previous is None or previous[1] == "known":
                return
            original = json.loads(previous[0])
            if original.get("source") == "frozen_raw_roi":
                return
            original["labelEvidence"] = evidence
            con.execute(
                "UPDATE research2_base43_samples SET roi=?,mature_date=?,label_status=?,evidence_json=? WHERE sample_id=? AND label_status<>'known'",
                (
                    float(roi) if status == "known" else None,
                    _date(mature_date),
                    status,
                    _json(original),
                    sample_id,
                ),
            )

    def mature_pending(self, inputs, day, next_session, costs, buy_terms, sell_terms):
        """Resolve every frozen candidate using the independent reference ticket."""
        day = _date(day)
        if not next_session:
            return {"resolved": 0, "unknown": 0, "pending": True}
        next_day = _date(next_session)
        if next_day <= day:
            raise ValueError("BASE43 reference maturity must follow entry session")
        with self.database.connection() as con:
            rows = con.execute(
                "SELECT sample_id,code FROM research2_base43_samples WHERE trade_date=? AND label_status<>'known' AND COALESCE(json_extract(evidence_json,'$.source'),'')<>'frozen_raw_roi' ORDER BY code",
                (day,),
            ).fetchall()
        resolved = unknown = 0
        for sample in rows:
            result = reference_label(inputs, sample[1], int(day), int(next_day), costs, buy_terms, sell_terms)
            known = bool(
                result["bought"] and (result["sold"] or result["marked"]) and np.isfinite(result["net_pct"])
            )
            evidence = {
                k: (None if isinstance(v, (float, np.floating)) and not np.isfinite(v) else v)
                for k, v in result.items()
            }
            self.mature(
                sample[0],
                result["net_pct"] if known else None,
                next_day,
                evidence,
                "known" if known else "unknown",
            )
            resolved += int(known)
            unknown += int(not known)
        return {"resolved": resolved, "unknown": unknown, "pending": False}


def feature_candidate(candidate):
    """Assemble causal inputs; qualifications and previous-session facts stay explicit."""
    c = candidate
    if not c.get("qualificationKnown") or not c.get("eligible") or c.get("priorStreak", -1) < 1:
        return None
    rows = c.get("auctionRows", [])
    ordered = sorted(rows, key=lambda r: r["time"])
    times = np.array([r["time"] for r in ordered], np.int32)
    values = np.array([r["fields"] for r in ordered], float).reshape(-1, 17)
    f, e = summarize_group(times, values)
    if not (np.isfinite(e[1]) and e[1] > 0 and 33900 <= e[4] < 34200 and e[7] == 0):
        return None
    auction = int(np.floor(e[1] * 100 + 0.500001))
    if (
        not c["lower"] <= auction <= c["upper"]
        or not np.isfinite(e[0])
        or abs(e[0] - c["reference"] / 100) > 0.005
    ):
        return None
    past = np.array(c.get("historicalAuctionVolumes", [])[-20:], float)
    valid = past[np.isfinite(past) & (past > 0)]
    f[3] = e[2] / valid.mean() if len(valid) >= 10 else np.nan
    p = np.zeros((1, 2, 241, 4), np.int32)
    v = np.full((1, 2, 241), np.nan)
    for row in c.get("previousBars", []):
        minute = row["time"]
        index = (
            0
            if minute == 570
            else minute - 570
            if 571 <= minute <= 690
            else minute - 660
            if 781 <= minute <= 900
            else -1
        )
        if index >= 0:
            p[0, 0, index] = row["ohlc"]
            v[0, 0, index] = row["volume"]
    meta = np.zeros((1, 2), dtype=[("ref", "i4"), ("upper", "i4"), ("label", "i1")])
    meta["label"] = -1
    meta[0, 0] = (c.get("previousReference", 0), c.get("previousUpper", 0), c.get("previousLabel", -1))
    previous = build_previous_features({"prices": p, "volumes": v, "meta": meta}, 1, [0])[0]
    x = np.r_[
        f.astype(np.float32),
        previous,
        np.float32(c["priorStreak"]),
        np.float32(0),
        np.float32(auction >= c["upper"]),
    ].astype(np.float32)
    x[~np.isfinite(x)] = np.nan
    return x


def reference_label(inputs, symbol, day, nextday, costs, buy_terms, sell_terms):
    """Original 30,000-yuan reference ticket, including first-unknown exit veto.

    This training target is independent of the live account waiting rule.
    No corporate-action adjustment is inferred from mismatching references.
    """
    row = {
        "date": int(day),
        "symbol": symbol,
        "maturity_date": int(nextday) if nextday else 99999999,
        "bought": False,
        "sold": False,
        "marked": False,
        "net_pct": np.nan,
        "exit_status": "no_buy",
    }
    rule = inputs.records.get((int(day), symbol), {})
    if not rule.get("known") or not rule.get("eligible"):
        row["exit_status"] = "unknown_entry_qualification"
        return row
    prices, volume = inputs.minute_arrays(symbol, day)
    price = int(prices[1, 0])
    lower = int(rule["lower"])
    upper = int(rule["upper"])
    if rule.get("suspended"):
        row["exit_status"] = "verified_suspension"
        return row
    if price <= 0 or lower <= 0 or not lower <= price < upper or not np.isfinite(volume[1]) or volume[1] < 0:
        return row
    qty = 3_000_000 // (price * 100) * 100
    while qty > 0 and buy_terms(price, qty, symbol=symbol, config=costs)[0] > 3_000_000:
        qty -= 100
    if qty <= 0 or volume[1] < qty:
        return row
    outlay = buy_terms(price, qty, symbol=symbol, config=costs)[0]
    row.update(bought=True, qty=int(qty), entry_price_cents=price, exit_status="unknown_outcome")
    if not nextday:
        return row
    nxt = inputs.records.get((int(nextday), symbol), {})
    if not nxt.get("known"):
        return row
    close = int(prices[-1, 3])
    reference = nxt.get("reference", nxt.get("ref", 0))
    if close <= 0 or reference != close:
        row["exit_status"] = "unknown_action_or_reference"
        return row
    if nxt.get("suspended"):
        row.update(
            marked=True, net_pct=(close * qty / outlay - 1) * 100, exit_status="unsold_verified_suspension"
        )
        return row
    p, v = inputs.minute_arrays(symbol, nextday)
    lo = int(nxt["lower"])
    up = int(nxt["upper"])
    for bar in range(1, 241):
        quote = int(p[bar, 0])
        vol = float(v[bar])
        if quote <= 0 or not lo <= quote <= up or not np.isfinite(vol) or vol < 0:
            row["exit_status"] = "unknown_exit_path"
            return row
        if quote > lo and vol >= qty:
            proceeds = sell_terms(quote, qty, symbol=symbol, config=costs)[0]
            row.update(
                sold=True,
                net_pct=(proceeds / outlay - 1) * 100,
                sell_bar=bar,
                exit_status="sold_at_open" if bar == 1 else "sold_after_retry",
            )
            return row
    mark = int(p[-1, 3])
    if lo <= mark <= up and mark > 0:
        row.update(marked=True, net_pct=(mark * qty / outlay - 1) * 100, exit_status="unsold_marked")
    return row
