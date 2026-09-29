"""Tencent/Sina quotes, stock master and strict exchange calendar."""

import hashlib
import json
import math
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from .common import (
    CN,
    MarketDataError,
    ProviderState,
    database_rows,
    instrument,
    number,
    remaining_seconds,
    timestamp,
)


def parse_tencent(text: str) -> list[dict]:
    quotes = []
    for code, payload in re.findall(r'v_(?:r_)?([a-z0-9_]+)="([^"]*)"', text):
        parts = [part.strip() for part in payload.split("~")]
        if len(parts) < 38:
            continue
        at = timestamp(parts[30] if len(parts[30]) == 14 or "/" in parts[30] else parts[29])
        quote = {
            "code": code,
            "name": parts[1],
            "price": number(parts[3]),
            "preClose": number(parts[4]),
            "open": number(parts[5]),
            "high": number(parts[33]),
            "low": number(parts[34]),
            "asOf": at.isoformat(),
            "source": "tencent",
            "status": "ok",
        }
        if any(quote[key] is None or quote[key] < 0 for key in ("price", "preClose", "open", "high", "low")):
            raise MarketDataError("Tencent quote contains invalid prices")
        high, low = quote["high"], quote["low"]
        if (high or low) and (
            not high
            or not low
            or high < low
            or any(value > 0 and not low <= value <= high for value in (quote["price"], quote["open"]))
        ):
            raise MarketDataError("Tencent OHLC is inconsistent")
        if code.startswith("hk"):
            quote.update(volume=number(parts[36]), amount=number(parts[37]))
        else:
            composite = parts[35].split("/")
            if len(composite) != 3 or number(composite[1]) is None or number(composite[2]) is None:
                raise MarketDataError("Tencent turnover fields are invalid")
            quote.update(volume=float(composite[1]) * 100, amount=float(composite[2]))
            for level in range(5):
                quote[f"bid{level + 1}"] = number(parts[9 + level * 2])
                quote[f"bidVolume{level + 1}"] = number(parts[10 + level * 2])
                quote[f"ask{level + 1}"] = number(parts[19 + level * 2])
                quote[f"askVolume{level + 1}"] = number(parts[20 + level * 2])
        quote["changePct"] = (quote["price"] / quote["preClose"] - 1) * 100 if quote["preClose"] else None
        if any(quote.get(key) is None or quote[key] < 0 for key in ("volume", "amount")):
            raise MarketDataError("Tencent turnover is invalid")
        quotes.append(quote)
    if not quotes:
        raise MarketDataError("Tencent returned no valid quotes")
    return quotes


def parse_sina(text: str) -> list[dict]:
    quotes = []
    for code, payload in re.findall(r'hq_str_([a-z0-9_.-]+)="([^"]*)"', text):
        values = [part.strip() for part in payload.split(",")]
        if code.startswith("hk") and len(values) >= 19:
            mapping = {
                "name": 1,
                "open": 2,
                "preClose": 3,
                "high": 4,
                "low": 5,
                "price": 6,
                "volume": 12,
                "amount": 11,
            }
            at = timestamp(values[17] + " " + values[18])
        elif code.startswith("gb_") and len(values) >= 35:
            mapping = {
                "name": 0,
                "open": 5,
                "preClose": 26,
                "high": 6,
                "low": 7,
                "price": 1,
                "volume": 10,
                "amount": 30,
            }
            at = datetime.fromisoformat(values[3]).replace(
                tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York")
            )
        elif code.startswith(("sh", "sz", "bj")) and len(values) >= 32:
            mapping = {
                "name": 0,
                "open": 1,
                "preClose": 2,
                "high": 4,
                "low": 5,
                "price": 3,
                "volume": 8,
                "amount": 9,
            }
            at = timestamp(values[30] + " " + values[31])
        else:
            continue
        quote = {
            key: values[index] if key == "name" else number(values[index]) for key, index in mapping.items()
        }
        if quote["price"] is None or quote["preClose"] is None:
            raise MarketDataError("Sina returned invalid quote prices")
        quote.update(code=code, asOf=at.astimezone(CN).isoformat(), source="sina", status="ok")
        quote["changePct"] = (quote["price"] / quote["preClose"] - 1) * 100 if quote["preClose"] else None
        if code.startswith(("sh", "sz", "bj")):
            for level in range(5):
                quote[f"bidVolume{level + 1}"] = number(values[10 + level * 2])
                quote[f"bid{level + 1}"] = number(values[11 + level * 2])
                quote[f"askVolume{level + 1}"] = number(values[20 + level * 2])
                quote[f"ask{level + 1}"] = number(values[21 + level * 2])
        quotes.append(quote)
    if not quotes:
        raise MarketDataError("Sina returned no valid quotes")
    return quotes


class Quotes(ProviderState):
    def quotes(self, codes: list[str], *, require_all=True, deadline=None) -> list[dict]:
        normalized = list(dict.fromkeys(instrument(code)["code"] for code in codes))
        found = {}
        failures = []
        for provider, url, parameter, parser in (
            ("tencent", "https://qt.gtimg.cn/", "q", parse_tencent),
            ("sina", "https://hq.sinajs.cn/", "list", parse_sina),
        ):
            missing = [code for code in normalized if code not in found]
            if provider == "tencent":
                missing = [code for code in missing if not code.startswith("gb_")]
            for offset in range(0, len(missing), 80):
                chunk = missing[offset : offset + 80]
                try:
                    rows = parser(self.http.text(url, {parameter: ",".join(chunk)}, encoding="gb18030", timeout=remaining_seconds(deadline)))
                    for quote in rows:
                        if quote["code"] in chunk:
                            found[quote["code"]] = quote
                except (MarketDataError, ValueError) as exc:
                    failures.append(f"{provider}: {exc}")
        missing = [code for code in normalized if code not in found]
        if missing and require_all or not found:
            raise MarketDataError(f"quotes unavailable for {', '.join(missing[:5])}; {'; '.join(failures)}")
        return [found[code] for code in normalized if code in found]

    def quote(self, code: str, *, deadline=None) -> dict:
        normalized = instrument(code)["code"]
        value = self.http.cached("quote:" + normalized, 2, lambda: self.quotes([normalized], deadline=deadline)[0])
        rate = 0.3 if normalized.startswith("bj") else 0.2 if normalized.startswith(("sh68", "sz30")) else 0.1
        if "ST" in value["name"].upper():
            rate = 0.05
        previous = value["preClose"]
        upper = math.floor(previous * (1 + rate) * 100 + 0.5) / 100 if previous else None
        lower = math.floor(previous * (1 - rate) * 100 + 0.5) / 100 if previous else None
        value.update(
            previousClose=previous,
            at=value["asOf"],
            quoteAt=value["asOf"],
            market=instrument(normalized)["market"],
            suspended=value.get("volume") == 0,
            limitUp=upper is not None and value["price"] >= upper - 0.001,
            limitDown=lower is not None and value["price"] <= lower + 0.001,
        )
        return value

    def stock_master(self) -> list[dict]:
        return database_rows(
            self.config.main_db, "SELECT * FROM tushare_stock_basic WHERE deleted_at IS NULL"
        )

    def stock_search(self, key: str) -> list[dict]:
        pattern = "%" + key + "%"
        output = database_rows(
            self.config.main_db,
            "SELECT * FROM tushare_stock_basic WHERE deleted_at IS NULL AND (name LIKE ? OR ts_code LIKE ?)",
            (pattern, pattern),
        )
        tables = {
            row["name"]
            for row in database_rows(self.config.main_db, "SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table, code_field, market in (
            ("tushare_index_basic", "ts_code", "A"),
            ("stock_base_info_hk", "code", "HK"),
            ("stock_base_info_us", "code", "US"),
        ):
            if table not in tables:
                continue
            for row in database_rows(
                self.config.main_db,
                f"SELECT * FROM {table} WHERE name LIKE ? OR {code_field} LIKE ?",
                (pattern, pattern),
            ):
                code = row[code_field]
                if market == "US":
                    code = code.lower().replace("us", "gb_", 1)
                output.append(
                    {
                        "ts_code": code,
                        "name": row["name"],
                        "fullname": row.get("full_name", row["name"]),
                        "market": row.get("market", market),
                        "symbol": row.get("symbol", ""),
                    }
                )
        return output

    def initialize_stock_master(self):
        if self.stock_master():
            return {"source": "persisted", "changed": False}
        return self.refresh_stock_master(allow_network=False)

    def refresh_stock_master(self, *, allow_network=True):
        required = {
            "ts_code",
            "symbol",
            "name",
            "market",
            "exchange",
            "list_status",
            "list_date",
            "curr_type",
        }

        def decode(payload):
            if payload.get("code") != 0:
                raise MarketDataError("stock master provider rejected request")
            data = payload.get("data") or {}
            fields = data.get("fields", [])
            if data.get("has_more") or not required <= set(fields):
                raise MarketDataError("partial stock master or missing fields")
            rows = [
                {key: str(value).strip() if value is not None else "" for key, value in zip(fields, row, strict=False)}
                for row in data.get("items", [])
            ]
            if len(rows) < 5000 or len({row.get("ts_code") for row in rows}) != len(rows):
                raise MarketDataError("stock master needs at least 5000 unique valid rows")
            if any(not all(row.get(key) for key in required) for row in rows):
                raise MarketDataError("stock master contains incomplete identities")
            return rows

        errors, rows, source = [], None, ""
        payload = None
        if allow_network and self.settings.get("tushareToken"):
            try:
                payload = self.http.json(
                    "https://api.tushare.pro",
                    method="POST",
                    body={
                        "api_name": "stock_basic",
                        "token": self.settings["tushareToken"],
                        "params": {},
                        "fields": "ts_code,symbol,name,area,industry,cnspell,market,list_date,act_name,act_ent_type,fullname,exchange,list_status,curr_type,enname,delist_date,is_hs",
                    },
                )
                rows, source = decode(payload), "tushare"
            except MarketDataError as exc:
                errors.append(str(exc))
        if allow_network and rows is None:
            endpoint = "https://raw.githubusercontent.com/yxforever666gh/stock-god/main/src/stock_god/market/resources/stock_basic.json"
            # Every GitHub request has an explicit local proxy; there is no direct retry.
            client = (
                self.http.client
                if not self.http.owns_client
                else httpx.Client(proxy="http://127.0.0.1:7890", trust_env=False, timeout=12)
            )
            try:
                response = client.get(endpoint)
                response.raise_for_status()
                payload = response.json()
                rows, source = decode(payload), "controlled_public"
            except (httpx.HTTPError, ValueError, MarketDataError) as exc:
                errors.append("controlled stock master unavailable: " + type(exc).__name__)
            finally:
                if client is not self.http.client:
                    client.close()
        if rows is None:
            if allow_network and self.stock_master():
                raise MarketDataError(
                    "stock master refresh failed; retained existing identities: " + "; ".join(errors)
                )
            payload = json.loads(
                (Path(__file__).with_name("resources") / "stock_basic.json").read_text(encoding="utf-8-sig")
            )
            rows, source = decode(payload), "packaged_seed"
        at = timestamp(datetime.now(CN)).isoformat()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        result = {
            "source": source,
            "rowCount": len(rows),
            "validRows": len(rows),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "fetchedAt": at,
            "usedSeed": source == "packaged_seed",
            "warnings": errors,
            "changed": True,
        }
        with sqlite3.connect(self.config.main_db, timeout=10) as connection:
            connection.execute("BEGIN IMMEDIATE")
            columns = {item[1] for item in connection.execute("PRAGMA table_info(tushare_stock_basic)")}
            if not required <= columns:
                raise MarketDataError("stock master schema is missing required columns")
            names = sorted(set(rows[0]) & columns)
            connection.execute("DELETE FROM tushare_stock_basic")
            connection.executemany(
                f"INSERT INTO tushare_stock_basic ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                [tuple(row.get(key, "") for key in names) for row in rows],
            )
            metadata_columns = {
                item[1] for item in connection.execute("PRAGMA table_info(stock_master_refresh_metadata)")
            }
            if metadata_columns:
                metadata = {
                    "id": 1,
                    "source": source,
                    "fetched_at": at,
                    "row_count": len(rows),
                    "valid_rows": len(rows),
                    "sha256": result["sha256"],
                    "used_seed": int(result["usedSeed"]),
                    "warnings": "\n".join(errors),
                }
                metadata = {key: val for key, val in metadata.items() if key in metadata_columns}
                names = list(metadata)
                connection.execute(
                    f"INSERT INTO stock_master_refresh_metadata({','.join(names)}) VALUES ({','.join('?' for _ in names)}) ON CONFLICT(id) DO UPDATE SET "
                    + ",".join(f"{key}=excluded.{key}" for key in names if key != "id"),
                    tuple(metadata.values()),
                )
            for table, resource in (
                ("stock_base_info_hk", "stock_base_info_hk.json"),
                ("stock_base_info_us", "stock_base_info_us.json"),
            ):
                columns = {item[1] for item in connection.execute(f"PRAGMA table_info({table})")}
                if not columns or connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    continue
                seeds = json.loads(
                    (Path(__file__).with_name("resources") / resource).read_text(encoding="utf-8-sig")
                )
                names = sorted((set(seeds[0]) & columns) - {"id"})
                connection.executemany(
                    f"INSERT INTO {table}({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                    [tuple(row.get(key) for key in names) for row in seeds],
                )
        return result

    def stock_snapshot(self, code: str) -> dict:
        quote = self.quote(code)
        mapping = {
            "code": "股票代码",
            "name": "股票名称",
            "price": "当前价格",
            "volume": "成交的股票数",
            "amount": "成交金额",
            "open": "今日开盘价",
            "preClose": "昨日收盘价",
            "high": "今日最高价",
            "low": "今日最低价",
        }
        output: dict = {label: str(quote.get(key, "")) for key, label in mapping.items()}
        at = timestamp(quote["asOf"])
        output.update(
            {
                "日期": at.date().isoformat(),
                "时间": at.strftime("%H:%M:%S"),
                "市场": "HK" if code.startswith("hk") else "US" if code.startswith("gb_") else "A",
                "changePercent": quote["changePct"],
                "changePrice": quote["price"] - quote["preClose"],
                "source": quote["source"],
                "asOf": quote["asOf"],
            }
        )
        for index, chinese in enumerate("一二三四五", 1):
            for key, label in (("bid", "买"), ("ask", "卖")):
                output[label + chinese + "报价"] = str(quote.get(f"{key}{index}", ""))
                output[label + chinese + "申报"] = str(quote.get(f"{key}Volume{index}", ""))
        # Legacy watchlist fields are optional read-only metadata. No follow-price update.
        table = database_rows(
            self.config.main_db, "SELECT name FROM sqlite_master WHERE type='table' AND name='followed_stock'"
        )
        if table:
            rows = database_rows(
                self.config.main_db,
                "SELECT * FROM followed_stock WHERE stock_code=? AND is_del=0 LIMIT 1",
                (quote["code"],),
            )
            if rows:
                for key, column in (
                    ("costPrice", "cost_price"),
                    ("costVolume", "volume"),
                    ("sort", "sort"),
                    ("alarmPrice", "alarm_price"),
                    ("alarmChangePercent", "alarm_change_percent"),
                ):
                    output[key] = rows[0].get(column)
        return output

    def is_trading_day(self, at: datetime, *, deadline=None) -> bool:
        day = timestamp(at)
        if day.weekday() >= 5:
            return False

        def load():
            token = self.settings.get("tushareToken", "")
            if not token:
                raise MarketDataError("trade calendar unavailable: Tushare token is empty")
            result = self.http.json(
                "https://api.tushare.pro",
                method="POST",
                body={
                    "api_name": "trade_cal",
                    "token": token,
                    "params": {
                        "exchange": "SSE",
                        "start_date": (day - timedelta(days=550)).strftime("%Y%m%d"),
                        "end_date": (day + timedelta(days=550)).strftime("%Y%m%d"),
                    },
                    "fields": "exchange,cal_date,is_open",
                },
                timeout=remaining_seconds(deadline),
            )
            if result.get("code") != 0:
                raise MarketDataError("trade calendar provider rejected request")
            data = result.get("data") or {}
            rows = [dict(zip(data.get("fields", []), row, strict=False)) for row in data.get("items", [])]
            if not rows:
                raise MarketDataError("empty trade calendar")
            return {
                datetime.strptime(row["cal_date"], "%Y%m%d").replace(tzinfo=CN).date().isoformat(): str(
                    row["is_open"]
                )
                == "1"
                for row in rows
            }

        calendar = self.http.cached("calendar:" + str(day.year), 21600, load)
        if day.date().isoformat() not in calendar:
            raise MarketDataError("trade calendar does not cover requested date")
        return calendar[day.date().isoformat()]

    def full_market(self) -> dict:
        fields = "f2,f3,f5,f6,f8,f12,f13,f14,f15,f16,f17,f18,f26,f62,f124"
        base = {
            "po": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fields": fields,
            "fs": "m:1+t:2,m:0+t:6,b:MK0021,b:MK0022,b:MK0023,b:MK0024",
        }
        errors = []

        def page(n, size, shape):
            for host in ("82.push2", "80.push2", "push2delay"):
                try:
                    response = self.http.json(
                        f"https://{host}.eastmoney.com/api/qt/clist/get",
                        base | {"pn": n, "pz": size, "np": shape},
                    )
                    data = response.get("data") or {}
                    diff = data.get("diff")
                    rows = list(diff.values()) if isinstance(diff, dict) else diff
                    if not rows or not data.get("total"):
                        raise MarketDataError("empty market universe")
                    return rows, int(data["total"])
                except (MarketDataError, TypeError, ValueError) as exc:
                    errors.append(str(exc))
            raise MarketDataError("Eastmoney full-market sources unavailable")

        try:
            try:
                rows, total = page(1, 20000, 2)
            except MarketDataError:
                rows, total = [], 1
            if len(rows) < total:
                rows, total = page(1, 100, 1)
                with ThreadPoolExecutor(max_workers=8) as pool:
                    for batch, _ in pool.map(
                        lambda index: page(index, 100, 1), range(2, (total + 99) // 100 + 1)
                    ):
                        rows.extend(batch)
            mapped = []
            for row in rows:
                code = str(row["f12"])
                mapped.append(
                    {
                        "code": instrument(code)["code"],
                        "name": row.get("f14", ""),
                        **{
                            key: number(row.get(field))
                            for key, field in {
                                "price": "f2",
                                "changePct": "f3",
                                "volume": "f5",
                                "amount": "f6",
                                "turnover": "f8",
                                "high": "f15",
                                "low": "f16",
                                "open": "f17",
                                "preClose": "f18",
                                "mainFlow": "f62",
                            }.items()
                        },
                        "listingDate": str(int(number(row.get("f26"), 0))),
                        "asOf": timestamp(row["f124"]).isoformat() if number(row.get("f124")) else None,
                    }
                )
            unique = {row["code"]: row for row in mapped}
            if len(unique) / total < 0.95:
                raise MarketDataError("full-market coverage below 95%")
            return {"rows": list(unique.values()), "reported": total, "source": "eastmoney", "errors": errors}
        except MarketDataError as exc:
            errors.append(str(exc))
        return self.fallback_full_market(errors)

    def fallback_full_market(self, errors=None):
        errors = list(errors or [])
        master = self.stock_master()
        codes = [
            instrument(row["ts_code"])["code"] for row in master if row.get("list_status") in (None, "", "L")
        ]
        if not codes:
            raise MarketDataError("no stock master for quote fallback")
        rows = []
        for offset in range(0, len(codes), 80):
            try:
                rows.extend(self.quotes(codes[offset : offset + 80], require_all=False))
            except MarketDataError as exc:
                errors.append(str(exc))
        if len(rows) / len(codes) < 0.95:
            raise MarketDataError("fallback full-market coverage below 95%")
        metadata = {instrument(row["ts_code"])["code"]: row for row in master}
        for row in rows:
            row["listingDate"] = metadata[row["code"]].get("list_date", "")
            row.setdefault("turnover", None)
            row.setdefault("mainFlow", None)
        return {"rows": rows, "reported": len(codes), "source": "tencent+sina", "errors": errors}
