"""Bounded model facts from frozen documents; no provider or database access."""

from __future__ import annotations

import copy
import json
import math
import re

from .core import json_text, parse_time

FIELDS = {
    "financials": (
        "REPORT_DATE NOTICE_DATE REPORT_TYPE CURRENCY TOTAL_OPERATE_INCOME NETPROFIT PARENT_NETPROFIT ROE DEBT_ASSET_RATIO TOTAL_ASSETS TOTAL_LIABILITIES",
        2,
    ),
    "concepts": ("BOARD_NAME NEW_BOARD_CODE BOARD_YIELD BOARD_RANK", 20),
    "flows": ("opendate trade changeratio netamount r0_net ratioamount", 5),
    "industry-rank": ("bd_code bd_name bd_zxj bd_zdf bd_zdf5 bd_zdf20 nzg_code nzg_name nzg_zdf", 10),
    "industry-money": (
        "category name avg_changeratio inamount outamount netamount ts_symbol ts_name ts_changeratio",
        10,
    ),
    "stock-money": ("symbol name trade changeratio amount netamount r0_net ratioamount", 10),
    "sector-flows": (
        "code name changePct netAmount inAmount outAmount mainNetRatio superLargeNetAmount largeNetAmount mediumNetAmount smallNetAmount",
        10,
    ),
    "concept-flows": (
        "code name changePct netAmount inAmount outAmount mainNetRatio superLargeNetAmount largeNetAmount mediumNetAmount smallNetAmount",
        10,
    ),
    "hot-stocks": ("symbol name current percent value increment rank_change", 10),
    "hot-topics": ("htid nickname desc stock_list", 5),
    "hot-events": ("id tag content", 5),
    "long-tiger": (
        "SECURITY_CODE SECURITY_NAME_ABBR TRADE_DATE CLOSE_PRICE CHANGE_RATE BILLBOARD_NET_AMT BILLBOARD_BUY_AMT BILLBOARD_SELL_AMT EXPLANATION",
        10,
    ),
    "gdp": ("REPORT_DATE TIME DOMESTICL_PRODUCT_BASE SUM_SAME FIRST_SAME SECOND_SAME THIRD_SAME", 3),
    "cpi": ("REPORT_DATE TIME NATIONAL_SAME NATIONAL_SEQUENTIAL NATIONAL_ACCUMULATE", 3),
    "ppi": ("REPORT_DATE TIME BASE BASE_SAME BASE_ACCUMULATE", 3),
    "pmi": ("REPORT_DATE TIME MAKE_INDEX NMAKE_INDEX MAKE_SAME NMAKE_SAME", 3),
}
EVENT_FIELDS = {
    "notices": "art_code title display_time notice_date content summary",
    "interactive": "mainContent attachedContent attachedPubDate stockCode companyShortName",
    "research": "title publishDate stockCode orgSName researcher emRatingName ratingChange epsThisYear epsNextYear peThisYear peNextYear summary",
    "related-news": "title content dataTime source",
    "market": "title content dataTime source",
    "reuters": "title headline description published_time first_publish_date display_date",
    "investment-calendar": "title content date eventAt time",
    "cls-calendar": "title calendar_time economic event holiday",
}
DATE_FIELDS = (
    "display_time",
    "attachedPubDate",
    "dataTime",
    "publishedAt",
    "publishDate",
    "publishTime",
    "published_time",
    "first_publish_date",
    "display_date",
    "eventAt",
    "calendar_time",
    "NOTICE_DATE",
    "notice_date",
    "TRADE_DATE",
    "opendate",
    "REPORT_DATE",
    "date",
    "time",
)


def numeric(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def event_time(row):
    for field in DATE_FIELDS:
        value = row.get(field)
        if value:
            # Eastmoney's disclosure timestamp ends in colon-separated milliseconds.
            text = re.sub(r"(\d{2}:\d{2}:\d{2}):(\d{3})$", r"\1.\2", str(value))
            at = parse_time(text)
            if at:
                return at
    return None


def records(value):
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for field in ("results", "items", "rows", "data", "result", "stories"):
            if isinstance(value.get(field), (dict, list)):
                return records(value[field])
        return [value]
    return []


def table(rows, fields):
    columns = [field for field in fields if any(field in row for row in rows)]
    return {"columns": columns, "rows": [[row.get(field) for field in columns] for row in rows]}


def table_metadata(columns, rows):
    units = {}
    for field in columns:
        lower = field.lower()
        units[field] = (
            "percent"
            if any(token in lower for token in ("pct", "ratio", "rate", "yield", "changeratio"))
            else "shares"
            if lower in {"volume", "vol", "shares"}
            else "CNY/share"
            if lower in {"price", "open", "high", "low", "close", "trade", "current"}
            else "CNY"
            if any(
                token in lower
                for token in ("amount", "income", "profit", "assets", "liabilities", "netamount", "netinflow")
            )
            else "provider_native"
        )
    status = {}
    missing_counts = {}
    for field in columns:
        missing = sum(row.get(field) is None for row in rows)
        missing_counts[field] = missing
        status[field] = "missing" if missing == len(rows) else "partial" if missing else "observed"
    return {"fieldStatus": status, "fieldMissingRows": missing_counts, "fieldUnits": units}


def relevant(row, codes, names, boards):
    tokens = {
        str(row.get(field, "")).lower()
        for field in ("code", "symbol", "SECURITY_CODE", "stockCode", "bd_code", "category", "NEW_BOARD_CODE")
    }
    links = row.get("stock_list")
    if isinstance(links, list):
        tokens.update(str(stock.get("code", "")).lower() for stock in links if isinstance(stock, dict))
    direct = bool(tokens & (codes | {code[-6:] for code in codes} | boards))
    text = " ".join(
        str(row.get(field, ""))
        for field in ("title", "content", "tag", "nickname", "desc", "name", "bd_name", "ts_name", "nzg_name")
    )
    return direct or any(name and name in text for name in names)


def daily_facts(rows, cutoff):
    valid, excluded = [], 0
    for row in rows:
        at = event_time(row)
        if not at or at.date() >= cutoff.date():
            excluded += 1
            continue
        prices = [numeric(row.get(key)) for key in ("open", "high", "low", "close")]
        if any(price is None or price <= 0 for price in prices):
            excluded += 1
            continue
        opening, high, low, close = prices
        if low > min(opening, close) or high < max(opening, close) or low > high:
            excluded += 1
            continue
        valid.append((at, row))
    valid.sort(key=lambda pair: pair[0])
    days = {}
    conflicts = set()
    for at, row in valid:
        day = at.date().isoformat()
        if day in days and any(
            days[day].get(k) != row.get(k) for k in ("open", "high", "low", "close", "volume")
        ):
            conflicts.add(day)
        days[day] = row
    ordered = [(day, row) for day, row in days.items() if day not in conflicts][-61:]
    recent, events, closes = [], [], []
    for index, (day, row) in enumerate(ordered):
        close = numeric(row["close"])
        previous = numeric(ordered[index - 1][1]["close"]) if index else None
        change = round((close / previous - 1) * 100, 4) if previous else None
        retreat = round((close / numeric(row["high"]) - 1) * 100, 4)
        amount = numeric(row.get("amount"))
        recent.append(
            {
                "date": day,
                **{k: numeric(row.get(k)) for k in ("open", "high", "low", "close", "volume")},
                "amount": amount if amount and amount > 0 else None,
                "returnPct": change,
                "highToClosePct": retreat,
            }
        )
        proven_previous, rate = numeric(row.get("preClose")), numeric(row.get("limitRate"))
        if proven_previous is not None and proven_previous > 0 and rate is not None and 0 < rate < 1:
            cents = math.floor(proven_previous * 100 + 0.5)
            recent[-1]["limitUpClose"] = (
                round(close * 100) == (cents * (10000 + round(rate * 10000)) + 5000) // 10000
            )
            recent[-1]["limitDownClose"] = (
                round(close * 100) == (cents * (10000 - round(rate * 10000)) + 5000) // 10000
            )
        closes.append(close)
        if change is not None and (abs(change) >= 8 or retreat <= -5):
            event = {"date": day, "close": close, "returnPct": change, "highToClosePct": retreat}
            event.update(
                {key: recent[-1][key] for key in ("limitUpClose", "limitDownClose") if key in recent[-1]}
            )
            events.append(event)
    statistics = {}
    for count in (5, 10, 20, 61):
        window = ordered[-count:]
        peak = max((numeric(row["high"]) for _, row in window), default=None)
        highest_close, drawdown = 0.0, 0.0
        for _, row in window:
            highest_close = max(highest_close, numeric(row["close"]))
            drawdown = min(drawdown, (numeric(row["close"]) / highest_close - 1) * 100)
        sources = {row.get("source") for _, row in window}
        volumes = [numeric(row.get("volume")) for _, row in window]
        comparable = len(window) == count and len(sources) == 1 and None not in sources and "" not in sources
        volume_ratio = None
        if comparable and all(v is not None and v > 0 for v in volumes) and count > 1:
            volume_ratio = round(volumes[-1] / (sum(volumes[:-1]) / (count - 1)), 4)
        statistics[str(count)] = {
            "observations": len(window),
            "returnPct": round((closes[-1] / closes[-count - 1] - 1) * 100, 4)
            if len(closes) > count
            else None,
            "distanceFromHighPct": round((closes[-1] / peak - 1) * 100, 4) if peak else None,
            "closeMaxDrawdownPct": round(drawdown, 4) if window else None,
            "sameSourceVolumeRatio": volume_ratio,
        }
    first_recent = recent[max(0, len(recent) - 20)]["date"] if recent else ""
    earlier_events = [event for event in events if event["date"] < first_recent]
    return {
        "status": "ok" if ordered else "unavailable",
        "observations": len(ordered),
        "from": ordered[0][0] if ordered else None,
        "through": ordered[-1][0] if ordered else None,
        "excludedRows": excluded + len(conflicts),
        "omittedRows": max(0, len(ordered) - 20),
        "recent": table(
            recent[-20:],
            "date open high low close volume amount returnPct highToClosePct limitUpClose limitDownClose".split(),
        ),
        "statistics": statistics,
        "earlierLargePriceChanges": earlier_events[-10:],
        "priceLimitRule": "only_verified_row_preClose_and_limitRate; percentage_alone_is_not_limit_proof",
        "volumeUnit": "provider_native; ratios_require_same_source",
    }


def project_source(document, cutoff, codes, names, boards):
    kind = document.get("sourceId", "").split(":")[-1]
    try:
        value = json.loads(document.get("content") or "null")
    except (ValueError, TypeError):
        return {"status": "unrecognized_structure"}
    rows = records(value)
    if kind == "daily":
        return daily_facts(rows, cutoff)
    if kind == "global-indexes" and isinstance(value, dict):
        unique = {}
        for group in value.values():
            for row in records(group):
                if row.get("qtcode"):
                    unique.setdefault(row["qtcode"], row)
        selected = list(unique.values())[:40]
        return {
            "status": "ok",
            **table(selected, "qtcode name location zxj zdf state".split()),
            **table_metadata("qtcode name location zxj zdf state".split(), selected),
            "omittedRows": len(unique) - len(selected),
        }
    if kind == "cls-calendar":
        rows = [row for parent in rows for row in records(parent.get("items", []))]
    if kind == "reuters":
        rows = [row for row in rows if row.get("title") or row.get("headline")]
    fields, limit = FIELDS.get(kind, (EVENT_FIELDS.get(kind, "").split(), 3))
    if kind == "interactive":
        limit = 2
    fields = fields.split() if isinstance(fields, str) else fields
    if not fields or not any(any(field in row for field in fields) for row in rows):
        return {"status": "empty" if not rows else "unrecognized_structure", "observations": len(rows)}
    safe = [row for row in rows if not event_time(row) or event_time(row) <= cutoff]
    indexed = list(enumerate(safe))
    dated = kind in {
        "financials",
        "flows",
        "notices",
        "interactive",
        "research",
        "related-news",
        "market",
        "reuters",
        "investment-calendar",
        "cls-calendar",
        "gdp",
        "cpi",
        "ppi",
        "pmi",
    }
    indexed.sort(
        key=lambda pair: (
            -int(relevant(pair[1], codes, names, boards)),
            -(event_time(pair[1]).timestamp() if dated and event_time(pair[1]) else 0),
            pair[0],
        )
    )
    selected = [copy.deepcopy(row) for _, row in indexed[:limit]]
    for row in selected:
        if isinstance(row.get("stock_list"), list):
            links = [stock for stock in row["stock_list"] if isinstance(stock, dict)]
            matched = [stock for stock in links if str(stock.get("code", "")).lower() in codes | boards]
            retained = matched or links[:3]
            row["stock_list"] = {
                "items": [{k: stock.get(k) for k in ("code", "name")} for stock in retained],
                "omitted": len(links) - len(retained),
            }
        if kind == "cls-calendar":
            for field in ("economic", "event", "holiday"):
                if isinstance(row.get(field), dict):
                    row[field] = {
                        k: row[field].get(k)
                        for k in ("title", "country", "front", "consensus", "actual", "unit")
                        if k in row[field]
                    }
    facts = {
        "status": "ok" if selected else "empty",
        **table(selected, fields),
        **table_metadata(fields, selected),
        "observations": len(safe),
        "omittedRows": max(0, len(safe) - len(selected)),
        "excludedRows": len(rows) - len(safe),
    }
    if kind == "notices":
        facts["bodyAvailable"] = any(row.get("content") or row.get("summary") for row in selected)
    if kind == "interactive":
        facts["timeField"] = "attachedPubDate"
    if kind in {"gdp", "cpi", "ppi", "pmi"}:
        facts["timeMeaning"] = "report_period; exact_release_time_not_proven"
    return facts


def compact_snapshot(evidence):
    try:
        snapshot = json.loads(evidence.get("prompt") or "null")
    except (ValueError, TypeError):
        return evidence.get("prompt", "")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("sources"), list):
        return evidence.get("prompt", "")
    snapshot = copy.deepcopy(snapshot)
    cutoff, freeze = parse_time(evidence.get("cutoffAt")), parse_time(evidence.get("freezeAt"))
    codes = {str(row.get("code", "")).lower() for row in evidence.get("candidates", [])}
    names = {str(row.get("name", "")) for row in evidence.get("candidates", [])}
    boards = {
        str(fact.get("NEW_BOARD_CODE", "")).lower()
        for proof in evidence.get("scoreEvidence", {}).values()
        for link in proof.get("sector", [])
        for fact in link.get("facts", [])
    }
    sources = []
    for document in evidence.get("documents", []):
        source_id = document.get("sourceId", "")
        available = parse_time(document.get("availableAt"))
        source = {
            "sourceId": source_id,
            "availableAt": document.get("availableAt"),
            "status": "failed" if document.get("error") else "ok",
        }
        if document.get("stockCode"):
            source["stockCode"] = document["stockCode"]
        if document.get("error"):
            source["error"] = document["error"]
        elif not available or not freeze or available > freeze:
            source["status"] = "time_unverified" if not available else "after_freeze"
        elif ":market:full" in source_id:
            source["factsIn"] = "market"
        elif ":quote:" in source_id or ":minutes:" in source_id:
            source["factsIn"] = "candidates"
        elif cutoff:
            source["facts"] = project_source(document, cutoff, codes, names, boards)
        sources.append(source)
    snapshot["sources"] = sources
    snapshot["inputFormat"] = "structured-source-facts-v1"
    return json_text(snapshot)


def model_score_evidence(proofs):
    return {
        stock_code: {
            key: [{field: value for field, value in link.items() if field != "facts"} for link in value]
            if key in {"sector", "catalyst"}
            else value
            for key, value in proof.items()
        }
        for stock_code, proof in proofs.items()
    }
