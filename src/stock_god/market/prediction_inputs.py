"""Frozen candidate snapshot and timestamped source documents for prediction."""

import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .common import CN, MarketDataError, ProviderState, instrument, now, number, timestamp
from .evidence import breadth_data


def metrics(bars, quote):
    result: dict[str, Any] = {
        key: None
        for key in (
            "returnPct",
            "vwap",
            "distanceFromHighPct",
            "maxDrawdownPct",
            "recoveryPct",
            "volumeAcceleration",
            "dayReturnPct",
            "dayOpenReturnPct",
            "dayDistanceFromHighPct",
        )
    }
    result.update(
        windowVolume=0.0, windowAmount=None, amountStatus="missing", historicalBaseline="unavailable"
    )
    if quote.get("preClose"):
        result["dayReturnPct"] = (quote["price"] / quote["preClose"] - 1) * 100
    if quote.get("preClose") and quote.get("open"):
        result["dayOpenReturnPct"] = (quote["open"] / quote["preClose"] - 1) * 100
    if quote.get("high"):
        result["dayDistanceFromHighPct"] = (quote["price"] / quote["high"] - 1) * 100
    if not bars:
        return result
    result["returnPct"] = (bars[-1]["close"] / bars[0]["open"] - 1) * 100
    volumes = [bar.get("volume") for bar in bars]
    result["windowVolume"] = (
        sum(volumes) if all(isinstance(value, (int, float)) for value in volumes) else None
    )
    amounts = [
        bar.get("amount") for bar in bars if isinstance(bar.get("amount"), (int, float)) and bar["amount"] > 0
    ]
    result["windowAmount"] = sum(amounts) if amounts else None
    result["amountStatus"] = "observed" if len(amounts) == len(bars) else "missing"
    if result["windowVolume"] is not None and result["windowVolume"] > 0:
        value = result["windowAmount"] / result["windowVolume"] if result["windowAmount"] is not None else 0
        low, high = min(bar["low"] for bar in bars), max(bar["high"] for bar in bars)
        if result["amountStatus"] == "observed" and low * 0.8 <= value <= high * 1.2:
            result["vwap"], result["vwapMethod"] = value, "amount_divided_by_share_volume"
        elif low * 0.8 <= value / 100 <= high * 1.2:
            result["vwap"], result["vwapMethod"] = value / 100, "amount_divided_by_lot_volume_times_100"
        else:
            result["vwap"] = sum(bar["close"] * bar["volume"] for bar in bars) / result["windowVolume"]
            result["vwapMethod"] = "volume_weighted_minute_close_proxy"
    high = max(bar["high"] for bar in bars)
    result["distanceFromHighPct"] = (bars[-1]["close"] / high - 1) * 100
    peak, trough, drawdown = 0.0, math.inf, 0.0
    for bar in bars:
        if bar["high"] > peak:
            peak, trough = bar["high"], bar["low"]
        else:
            trough = min(trough, bar["low"])
        drawdown = min(drawdown, (bar["low"] / peak - 1) * 100)
    result["maxDrawdownPct"] = drawdown
    if peak > trough:
        result["recoveryPct"] = (bars[-1]["close"] - trough) / (peak - trough) * 100
    middle = len(bars) // 2
    first_values = [bar.get("volume") for bar in bars[:middle]]
    second_values = [bar.get("volume") for bar in bars[middle:]]
    first = sum(first_values) if all(isinstance(value, (int, float)) for value in first_values) else 0
    second = sum(second_values) if all(isinstance(value, (int, float)) for value in second_values) else 0
    if first:
        result["volumeAcceleration"] = (second / max(1, len(bars) - middle)) / (first / max(1, middle))
    return {key: round(value, 4) if isinstance(value, float) else value for key, value in result.items()}


def _object_times(value):
    result = []
    for key, raw in value.items():
        if key.lower() in {"time", "datatime", "asof", "at"} or any(
            token in key.lower()
            for token in (
                "publish",
                "eventat",
                "event_at",
                "availableat",
                "available_at",
                "notice_date",
                "datetime",
                "date_time",
                "timestamp",
                "trade_time",
                "tradedate",
                "trade_date",
                "日期",
            )
        ):
            try:
                result.append(timestamp(raw))
            except (ValueError, TypeError, OverflowError):
                continue
    return result


def _has_observation_time(value):
    if isinstance(value, list):
        return any(_has_observation_time(item) for item in value)
    if isinstance(value, dict):
        return bool(_object_times(value)) or any(_has_observation_time(item) for item in value.values())
    return False


def normalize_auxiliary(value, cutoff, collected_at, *, source=""):
    """Undated snapshots use collection time; historical hot topics require a same-day observation."""
    if not _has_observation_time(value):
        if source == "hot-topics" and timestamp(collected_at).date() > timestamp(cutoff).date():
            return [], None, False
        return value, collected_at, value not in (None, "", [], {})
    return filter_at_cutoff(value, cutoff)


def eligible_coverage(rows, collected_at):
    """Use the original main-board denominator, retaining unobserved eligible symbols."""
    eligible, best = set(), {}
    latest = timestamp(collected_at) + timedelta(seconds=5)
    for row in rows:
        try:
            code = instrument(str(row.get("code", "")))["code"]
        except ValueError:
            continue
        name = str(row.get("name") or "").upper()
        if (
            not code.startswith(("sh60", "sz00"))
            or "ST" in name
            or "退" in name
            or number(row.get("listingDate"), 0) <= 0
        ):
            continue
        eligible.add(code)
        try:
            at = timestamp(row["asOf"])
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
        if number(row.get("price"), 0) <= 0 or at > latest:
            continue
        old = best.get(code)
        if old is None or (at, number(row.get("amount"), 0)) > (
            timestamp(old["asOf"]),
            number(old.get("amount"), 0),
        ):
            best[code] = row | {"code": code}
    return [best[code] for code in sorted(best)], len(eligible)


def filter_at_cutoff(value, cutoff, inherited=False):
    """A dated child never makes its untimestamped siblings eligible."""
    if isinstance(value, list):
        items = [filter_at_cutoff(item, cutoff, inherited) for item in value]
        valid = [(item, at) for item, at, keep in items if keep]
        return (
            [item for item, _ in valid],
            max((at for _, at in valid if at), default=None),
            bool(valid) or inherited,
        )
    if isinstance(value, dict):
        direct = _object_times(value)
        if any(at > cutoff for at in direct):
            return None, max(direct), False
        result, dates = {}, list(direct)
        for key, item in value.items():
            filtered, at, keep = filter_at_cutoff(item, cutoff, inherited or bool(direct))
            if keep:
                result[key] = filtered
                if at:
                    dates.append(at)
        return result, max(dates, default=None), inherited or bool(direct) or bool(result)
    return value, None, inherited


class PredictionInputs(ProviderState):
    def collect_prediction_evidence(self, cutoff: datetime, exclusions, cash: float):
        try:
            return self._collect_prediction_evidence(cutoff, exclusions, cash)
        except MarketDataError as exc:
            details = exc.evidence or {}
            if exc.evidence is None or "candidates" not in exc.evidence:
                at = timestamp(cutoff).isoformat()
                exc.evidence = {
                    "availableCash": cash,
                    "cutoffAt": at,
                    "freezeAt": now().isoformat(),
                    "windowStartAt": (timestamp(cutoff) - timedelta(minutes=5)).isoformat(),
                    "windowEndAt": at,
                    "evidenceProfileVersion": "research2-slots-v8",
                    "degraded": True,
                    "degradedReasons": [str(exc)],
                    "coveragePct": 0,
                    "candidates": [],
                    "candidateReferencePrices": {},
                    "prompt": "",
                    "sourceStatusJson": json.dumps(
                        [{"sourceId": "research2:market:full", "status": "failed", "error": str(exc)}]
                    ),
                    "documents": [
                        {
                            "sourceId": "research2:market:full",
                            "sourceName": "全市场候选快照",
                            "category": "market",
                            "collectedAt": now().isoformat(),
                            "availableAt": None,
                            "content": "",
                            "error": str(exc),
                        }
                    ],
                }
                exc.evidence.update(details)
            raise

    def _collect_prediction_evidence(self, cutoff: datetime, exclusions, cash: float):
        started = timestamp(cutoff)
        snapshot = self.full_market()
        raw_reported = snapshot["reported"]
        rows, eligible_reported = eligible_coverage(
            snapshot["rows"], timestamp(snapshot.get("collectedAt") or now())
        )
        coverage = len(rows) / eligible_reported if eligible_reported else 0
        if coverage < 0.95 and snapshot["source"] != "tencent+sina":
            try:
                fallback = self.fallback_full_market(snapshot.get("errors", []))
                fallback_rows, fallback_reported = eligible_coverage(
                    fallback["rows"], timestamp(fallback.get("collectedAt") or now())
                )
                if fallback_reported and len(fallback_rows) / fallback_reported >= 0.95:
                    snapshot, rows, eligible_reported = fallback, fallback_rows, fallback_reported
                    raw_reported = fallback["reported"]
                    coverage = len(rows) / eligible_reported
            except MarketDataError:
                pass
        if coverage < 0.95:
            message = (
                f"eligible market coverage {coverage * 100:.2f}% below 95% ({len(rows)}/{eligible_reported})"
            )
            failure = MarketDataError(message)
            # The public wrapper fills all standard failure fields; retain the measured evidence here.
            failure.evidence = {
                "coveragePct": coverage * 100,
                "sourceReported": raw_reported,
                "eligibleReported": eligible_reported,
                "observed": len(rows),
            }
            raise failure
        snapshot = snapshot | {"rows": rows, "reported": eligible_reported, "sourceReported": raw_reported}
        dates = [timestamp(row["asOf"]) for row in rows if row.get("asOf")]
        if not dates:
            raise MarketDataError("market snapshot has no verifiable observation timestamp")
        cutoff = max(dates)
        if cutoff > now() + timedelta(seconds=5):
            raise MarketDataError("market snapshot timestamp is in the future")
        window_end = cutoff.replace(second=0, microsecond=0)
        if (11, 30) <= (started.hour, started.minute) < (13, 0) or (11, 30) <= (
            cutoff.hour,
            cutoff.minute,
        ) < (13, 0):
            window_end = cutoff.replace(hour=11, minute=30, second=0, microsecond=0)
        if window_end.hour >= 15:
            window_end = cutoff.replace(hour=15, minute=0, second=0, microsecond=0)
        window_start = window_end - timedelta(minutes=5)
        minimum_bars = 0 if (9, 30) <= (cutoff.hour, cutoff.minute) < (9, 35) else 4
        if minimum_bars == 0:
            window_start = cutoff.replace(hour=9, minute=30, second=0, microsecond=0)
        excluded = {
            str(code).lower().replace(".sh", "").replace(".sz", "").removeprefix("sh").removeprefix("sz")
            for code in exclusions
        }
        eligible = []
        for row in rows:
            code, price, previous = row["code"], row.get("price"), row.get("preClose")
            if (
                not code.startswith(("sh60", "sz00"))
                or code[2:] in excluded
                or "ST" in row["name"].upper()
                or "退" in row["name"]
            ):
                continue
            if (
                not price
                or price <= 0
                or not previous
                or previous <= 0
                or not row.get("volume")
                or row["volume"] <= 0
                or not row.get("amount")
                or row["amount"] <= 0
                or not row.get("changePct")
                or row["changePct"] <= 0
            ):
                continue
            if not row.get("asOf") or not cutoff - timedelta(minutes=3) <= timestamp(row["asOf"]) <= cutoff:
                continue
            try:
                listed = datetime.strptime(row.get("listingDate", ""), "%Y%m%d").replace(tzinfo=CN)
            except ValueError:
                continue
            if listed > cutoff:
                continue
            if (cutoff - listed).days < 45:
                sessions = sum(
                    self.is_trading_day(listed + timedelta(days=offset))
                    for offset in range((cutoff - listed).days + 1)
                )
                if sessions < 10:
                    continue
            upper = float(
                (Decimal(str(previous)) * Decimal("1.1")).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
            )
            if (upper - price) / upper * 100 < 1.5 - 1e-7:
                continue
            notional = price * 100
            lot_cost = (
                notional + max(5, notional * 0.0002) + (notional * 0.00001 if code.startswith("sh") else 0)
            )
            if lot_cost > cash:
                continue
            eligible.append(row)
        eligible.sort(
            key=lambda row: (
                row["changePct"] * 4 + math.log10(max(1, row["amount"])) * 2 + (row.get("turnover") or 0)
            ),
            reverse=True,
        )
        eligible = eligible[:12]
        documents, compact, candidates, reasons = [], [], [], []

        def document(source_id, category, value, available, error="", code="", source="", ref=""):
            collected = now().isoformat()
            at = timestamp(available).isoformat() if available else None
            return {
                "sourceId": source_id,
                "sourceName": source or source_id,
                "sourceRef": ref,
                "category": category,
                "collectedAt": collected,
                "retrievedAt": collected,
                "availableAt": at,
                "publishedAt": at,
                "content": json.dumps(value, ensure_ascii=False, separators=(",", ":")) if not error else "",
                "error": error,
                "stockCode": code,
                "collectionStatus": "failed" if error else "ok",
            }

        documents.append(
            document("research2:market:full", "market", snapshot, cutoff, source=snapshot["source"])
        )

        def load_window(row):
            try:
                bars = [
                    bar
                    for bar in self.prediction_window(row["code"], window_start, window_end, minimum_bars)
                    if timestamp(bar["time"]) < window_end
                ]
                if len(bars) < minimum_bars:
                    raise MarketDataError("fewer than four closed minute bars")
                return row, bars, ""
            except MarketDataError as exc:
                return row, [], str(exc) if minimum_bars else ""

        with ThreadPoolExecutor(max_workers=6) as pool:
            windows = list(pool.map(load_window, eligible))
        for row, bars, error in windows:
            code = row["code"]
            source_id = "research2:minutes:" + code
            quote = {
                "at": row["asOf"],
                "price": row["price"],
                "open": row["open"],
                "previousClose": row["preClose"],
                "high": row["high"],
                "low": row["low"],
                "turnoverPct": row.get("turnoverPct", row.get("turnover")),
                "mainFlowCny": row.get("mainFlowCny", row.get("mainFlow")),
                "mainFlow": row.get("mainFlowCny", row.get("mainFlow")),
                "dayVolume": row["volume"],
                "dayAmount": row["amount"],
            }
            documents.append(
                document(
                    "research2:quote:" + code,
                    "stock",
                    {"entityId": "stock:" + code, "quote": quote},
                    row["asOf"],
                    code=code,
                    source="截止点行情 " + code,
                )
            )
            documents.append(
                document(
                    source_id,
                    "stock",
                    {
                        "entityId": "stock:" + code,
                        "windowStartAt": window_start.isoformat(),
                        "cutoffAt": cutoff.isoformat(),
                        "bars": bars,
                        "metrics": metrics(bars, row),
                    },
                    bars[-1]["time"] if bars else cutoff,
                    error,
                    code,
                    "五分钟未复权行情 " + code,
                )
            )
            compact.append(
                {
                    "entityId": "stock:" + code,
                    "code": code,
                    "name": row["name"],
                    "coreEligible": not bool(error),
                    "quote": quote,
                    "minuteBarCount": len(bars),
                    "minuteSource": bars[0]["source"] if bars else "",
                    "metrics": metrics(bars, row),
                    "sourceIds": ["research2:quote:" + code, source_id],
                    "missing": ([error] if error else []) + ["historical_5day_same_time_baseline"],
                }
            )
            if error:
                reasons.append(code + ": " + error)
            else:
                candidates.append({"code": code, "name": row["name"], "referencePrice": row["price"]})
                reasons.append("historical_5day_same_time_baseline_unavailable:" + code)
        market = breadth_data(rows) | {
            "sourceId": "research2:market:full",
            "observed": len(rows),
            "reported": snapshot["reported"],
            "sourceReported": snapshot["sourceReported"],
            "coveragePct": len(rows) / snapshot["reported"] * 100,
            "sectorFlows": [],
            "conceptFlows": [],
        }
        for scope in ("sector", "concept"):
            flow = self.fund_flows(scope)
            error = ""
            at = None if flow["asOf"].startswith("0001") else timestamp(flow["asOf"])
            if flow["status"] == "unavailable" or at is None or at > cutoff:
                error = "fund-flow source has no cutoff-safe observation"
                reasons.append(scope + ": " + error)
            else:
                market[scope + "Flows"] = flow["data"]
            documents.append(
                document(
                    "research2:market:" + scope + "-flows", "sector", flow, at, error, source=flow["source"]
                )
            )
        accepted = []
        try:
            news = []
            for source in ("财联社电报", "新浪财经"):
                try:
                    news.extend(self._live_news(source))
                except (MarketDataError, ValueError, KeyError) as exc:
                    reasons.append(source + ": " + str(exc))
            try:
                news.extend(self.telegraphs(limit=1000))
            except MarketDataError as exc:
                reasons.append("news cache: " + str(exc))
            accepted = [
                row
                for row in news
                if row.get("dataTime") and cutoff - timedelta(days=1) <= timestamp(row["dataTime"]) <= cutoff
            ]
            documents.append(
                document(
                    "research2:news:market",
                    "news",
                    accepted,
                    max((row["dataTime"] for row in accepted), default=None),
                    "no cutoff-safe news" if not accepted else "",
                    source="市场新闻",
                )
            )
        except MarketDataError as exc:
            reasons.append(str(exc))
        jobs = [
            ("global-indexes", "market", "全球与国内指数", "", self.global_indexes),
            ("reuters", "market", "Reuters", "", self.reuters_news),
            (
                "investment-calendar",
                "market",
                "九阳公社投资日历",
                "",
                lambda: self.investment_calendar(cutoff.strftime("%Y-%m")),
            ),
            ("cls-calendar", "market", "财联社投资日历", "", self.cls_calendar),
            ("industry-rank", "sector", "腾讯行业排名", "", lambda: self.industry_rank("changepercent", 30)),
            ("industry-money", "sector", "Sina行业资金", "", lambda: self.money_rank("gn", "netamount")),
            ("stock-money", "sector", "Sina个股资金", "", lambda: self.money_rank(stocks=True)),
            ("hot-stocks", "sector", "雪球沪深热股", "", self.hot_stocks),
            ("hot-events", "sector", "雪球热点事件", "", lambda: self.hot_events(30)),
            ("hot-topics", "sector", "东方财富热门话题", "", lambda: self.hot_topics(30)),
            (
                "long-tiger",
                "sector",
                "东方财富龙虎榜",
                "",
                lambda: self.long_tiger(cutoff.date().isoformat()),
            ),
        ]
        for indicator in ("GDP", "CPI", "PPI", "PMI"):
            jobs.append(
                (
                    indicator.lower(),
                    "market",
                    "东方财富" + indicator,
                    "",
                    lambda indicator=indicator: self.macro_evidence(indicator),
                )
            )
        for candidate in candidates:
            code, name = candidate["code"], candidate["name"]
            for key, label, call in (
                ("notices", "东方财富公告", lambda code=code: self.notices(code)),
                ("research", "东方财富研报", lambda code=code: self.research_reports(code)),
                ("financials", "东方财富财务", lambda code=code: self.stock_financials(code)),
                ("concepts", "东方财富概念", lambda code=code: self.stock_concepts(code)),
                ("flows", "Sina资金流", lambda code=code: self.money_trend(code, 10)),
                ("interactive", "巨潮互动易", lambda name=name: self.interactive_answers(name)),
                (
                    "daily",
                    "Sina日K",
                    lambda code=code: self.bars(
                        code, cutoff - timedelta(days=120), cutoff, "day", "none", 61
                    ),
                ),
                (
                    "related-news",
                    "相关市场新闻",
                    lambda code=code, name=name: [
                        row
                        for row in accepted
                        if code[2:] in row.get("content", "") or name in row.get("content", "")
                    ],
                ),
            ):
                jobs.append((code + ":" + key, "stock", label + " " + code, code, call))

        def auxiliary(job):
            key, category, label, code, call = job
            try:
                raw = call()
                completed_at = now()
                filtered, available, keep = normalize_auxiliary(raw, cutoff, completed_at, source=key)
                error = "source has no cutoff-safe items" if not keep else ""
                result = document("research2:aux:" + key, category, filtered, available, error, code, label)
                if not _has_observation_time(raw):
                    result["publishedAt"] = None
                return result
            except (MarketDataError, ValueError, KeyError, TypeError) as exc:
                return document("research2:aux:" + key, category, [], None, str(exc), code, label)

        with ThreadPoolExecutor(max_workers=12) as pool:
            documents.extend(pool.map(auxiliary, jobs))
        if self.settings.get("experimentalEvidenceEnabled"):
            try:
                documents.extend(self.prediction_theme_documents(cutoff))
            except MarketDataError as exc:
                documents.append(document("research2:themes", "theme", [], None, str(exc)))
        for doc in documents:
            if doc["error"]:
                reasons.append("source_failed:" + doc["sourceId"])
        sources = [
            {
                "sourceId": doc["sourceId"],
                "sourceName": doc["sourceName"],
                "category": doc["category"],
                "status": "failed" if doc["error"] else "ok",
                "availableAt": doc["availableAt"],
                "error": doc["error"],
                "summary": doc["content"][:480],
            }
            for doc in documents
        ]
        payload = {
            "version": "research2-slots-v8",
            "windowStartAt": window_start.isoformat(),
            "windowEndAt": window_end.isoformat(),
            "cutoffAt": cutoff.isoformat(),
            "freezeAt": max(
                [now(), cutoff] + [timestamp(doc["collectedAt"]) for doc in documents]
            ).isoformat(),
            "market": market,
            "candidates": compact,
            "sources": sources,
            "degraded": bool(reasons),
            "degradedReasons": reasons,
        }
        return {
            "availableCash": cash,
            "prompt": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            "sourceStatusJson": json.dumps(sources, ensure_ascii=False),
            "candidates": candidates,
            "documents": documents,
            "evidenceProfileVersion": "research2-slots-v8",
            "cutoffAt": cutoff.isoformat(),
            "freezeAt": payload["freezeAt"],
            "windowStartAt": window_start.isoformat(),
            "windowEndAt": window_end.isoformat(),
            "coveragePct": market["coveragePct"],
            "degraded": bool(reasons),
            "degradedReasons": reasons,
            "candidateReferencePrices": {row["code"]: row["referencePrice"] for row in candidates},
        }
