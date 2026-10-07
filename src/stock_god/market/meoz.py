"""MeoZ contracted facts transport; no strategy or credential logging."""

import math
import re
import time
from copy import deepcopy
from datetime import datetime

import httpx

from .common import CN, MarketDataError, instrument, number, timestamp

LIVE = ("https://sz.meoz.cn:6688/api", "https://sh.meoz.cn:6688/api")
HISTORY = "https://hist.meoz.cn:6688/api"
APIS = {"tick_history", "daily_auc_detail", "pricelimit", "stockbasic", "suspend", "limit_pool_yes"}
TICK_FIELDS = "tradedate,symbol,time,close,vol,amount,transaction_num,bid1,bid2,ask1,ask2,bid_vol1,bid_vol2,ask_vol1,ask_vol2"


class MeozError(MarketDataError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class MeozProvider:
    """Immutable per-task settings, bounded retries and validated array records."""

    def __init__(self, settings, client, *, clock=None, sleep=None):
        self.settings = deepcopy(settings)
        self.client = client
        self.clock = clock or (lambda: datetime.now(CN))
        self.sleep = sleep or time.sleep
        self.node = 0

    @property
    def configured(self):
        return bool(self.settings.get("meozApiKey", "").strip())

    def status(self):
        return {"status": "unverified" if self.configured else "unconfigured",
                "configured": self.configured, "ready": False,
                "message": "竞价来源待核验" if self.configured else "竞价 API 未配置，未执行选股"}

    def _remaining(self, deadline):
        remaining = 10.0 if deadline is None else (timestamp(deadline) - self.clock()).total_seconds()
        if remaining <= 0:
            raise MeozError("incomplete", "MeoZ 接收截止时间已过")
        return min(10.0, remaining)

    def request(self, apiname, params, fields=None, *, history=False, deadline=None):
        if apiname not in APIS:
            raise ValueError("unsupported MeoZ interface")
        if history and apiname != "tick_history":
            raise ValueError("interface has no contracted MeoZ history service")
        if not self.configured:
            raise MeozError("unconfigured", "竞价 API 未配置")
        body = {"apikey": self.settings["meozApiKey"], "apiname": apiname,
                "params": deepcopy(params)}
        if fields is not None:
            body["fields"] = fields
        switched = False
        for attempt in range(3):
            url = HISTORY if history else LIVE[self.node]
            try:
                response = self.client.post(url, json=body, timeout=self._remaining(deadline))
            except (httpx.TimeoutException, httpx.ConnectError):
                if not history and not switched:
                    self.node = 1 - self.node
                    switched = True
                    continue
                raise MeozError("incomplete", "MeoZ 连接失败或超时") from None
            except httpx.HTTPError:
                raise MeozError("incomplete", "MeoZ 传输失败") from None
            received = self.clock()
            if deadline is not None and received > timestamp(deadline):
                raise MeozError("incomplete", "MeoZ 数据在截止后接收")
            try:
                payload = response.json() if response.status_code == 200 else {}
            except ValueError:
                raise MeozError("incomplete", "MeoZ 响应不是有效 JSON") from None
            if not isinstance(payload, dict):
                raise MeozError("incomplete", "MeoZ 响应结构错误")
            code = payload.get("code", response.status_code)
            if code in (401, 403):
                raise MeozError("no_permission", "MeoZ 密钥无效或接口无权限")
            if code == 429 or isinstance(code, int) and 500 <= code <= 599:
                if attempt == 2:
                    raise MeozError("incomplete", "MeoZ 限流或服务暂不可用")
                delay = number(response.headers.get("Retry-After"), float(2 ** attempt))
                delay = max(0.0, min(30.0, delay))
                if deadline is not None and delay >= self._remaining(deadline):
                    raise MeozError("incomplete", "MeoZ 重试超出接收截止时间")
                self.sleep(delay)
                continue
            # The live suspension service uses 1002/null for an empty result.
            if (response.status_code == 200 and apiname == "suspend" and code == 1002
                    and payload.get("message") == "未找到停牌复牌数据" and payload.get("data") is None):
                return {"rows": [], "fields": fields.split(",") if fields else [],
                        "source": url, "receivedAt": received.isoformat()}
            if response.status_code != 200 or code != 200:
                raise MeozError("incomplete", "MeoZ 接口请求失败")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise MeozError("incomplete", "MeoZ 未返回结构化事实")
            names, items = data.get("fields"), data.get("items")
            if (not isinstance(names, list) or not all(isinstance(x, str) for x in names)
                    or len(set(names)) != len(names) or not isinstance(items, list)):
                raise MeozError("incomplete", "MeoZ 字段数组无效")
            rows = []
            for values in items:
                if not isinstance(values, list) or len(values) != len(names):
                    raise MeozError("incomplete", "MeoZ 行与字段不匹配")
                row = dict(zip(names, values, strict=True))
                if "symbol" in row and (not isinstance(row["symbol"], str)
                                        or not re.fullmatch(r"\d{6}", row["symbol"])):
                    raise MeozError("incomplete", "MeoZ 股票代码无效")
                if "tradedate" in row:
                    day = row["tradedate"]
                    try:
                        if not isinstance(day, str) or not re.fullmatch(r"\d{8}", day):
                            raise ValueError()
                        datetime.strptime(day, "%Y%m%d")
                    except ValueError:
                        raise MeozError("incomplete", "MeoZ 交易日期无效") from None
                    if params.get("tradedate") and day != params["tradedate"]:
                        raise MeozError("incomplete", "MeoZ 返回非请求交易日")
                rows.append(row)
            return {"rows": rows, "fields": names, "source": url,
                    "receivedAt": received.isoformat()}
        raise MeozError("incomplete", "MeoZ 重试耗尽")

    def ticks(self, day, symbols, *, final=False, deadline=None):
        symbols = [instrument(s)["code"][2:] for s in symbols]
        records = []
        for offset in range(0, len(symbols), 200):
            result = self.request("tick_history", {"asset": "stock", "symbols": symbols[offset:offset + 200],
                "tradedate": day.replace("-", ""), "limit": 200,
                "side": "after" if final else "before", "trademin": "0925" if final else "0930"},
                TICK_FIELDS, deadline=deadline)
            for raw in result["rows"]:
                if raw["symbol"] not in symbols[offset:offset + 200]:
                    raise MeozError("incomplete", "MeoZ 返回非请求股票")
                try:
                    quote_time = timestamp(raw["time"])
                except (ValueError, TypeError, KeyError):
                    raise MeozError("incomplete", "MeoZ 快照时间无效") from None
                received = timestamp(result["receivedAt"])
                if (quote_time.date() != datetime.strptime(day.replace("-", ""), "%Y%m%d").date()
                        or quote_time > received or quote_time.strftime("%H%M%S") >= "093000"
                        or quote_time.strftime("%H%M%S") < ("092500" if final else "091500")):
                    raise MeozError("incomplete", "MeoZ 快照不在有效竞价时段")
                records.append({"raw": raw, "code": instrument(raw["symbol"])["code"],
                                "asOf": quote_time.isoformat(), "availableAt": received.isoformat(),
                                "source": result["source"]})
        return records

    @staticmethod
    def auction_values(raw, reference_price, *, volume_unit=None):
        """Research's 17-column layout; unknown cumulative volume remains NaN."""
        def n(field):
            return number(raw.get(field), math.nan)

        def shares(field):
            return n(field) * 100
        volume = n("vol") * (1 if volume_unit == "share" else 100 if volume_unit == "lot" else math.nan)
        return [reference_price, n("close"), volume, n("amount"), n("transaction_num"),
                n("bid1"), shares("bid_vol1"), n("bid2"), shares("bid_vol2"),
                n("ask1"), shares("ask_vol1"), n("ask2"), shares("ask_vol2"),
                math.nan, math.nan, math.nan, math.nan]
