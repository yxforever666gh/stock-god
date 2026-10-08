"""Bounded MeoZ Tick subscription; credentials never leave the transport."""

import asyncio
import inspect
import json
import logging
import math
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .common import instrument, number, timestamp
from .meoz import MeozError

FIELDS = ("symbol", "time", "lastPrice", "volume", "amount", "bidPrice", "askPrice", "bidVol", "askVol", "send_ts_ms")
_LOGGER = logging.Logger("stock-god-meoz-socket", level=logging.CRITICAL)
_LOGGER.disabled = True  # WebSocket debug handshakes contain the signed ticket URL.


def subscription_symbols(provider, *, deadline=None):
    result = provider.request("stockbasic", {"market": "主板", "list_status": "L"},
                              "code,symbol,name,market,exchange,list_status", deadline=deadline, budget_seconds=10)
    selected = set()
    for row in result["rows"]:
        if not all(isinstance(row.get(k), str) for k in ("symbol", "name", "market", "list_status")):
            raise MeozError("incomplete", "订阅股票资料字段不完整")
        if row["market"] != "主板" or row["list_status"] != "L" or "ST" in row["name"].upper():
            continue
        explicit = row.get("code")
        exchange = row.get("exchange")
        if isinstance(explicit, str) and explicit.upper().endswith((".SH", ".SZ")):
            code = instrument(explicit)["code"]
            if code[2:] != row["symbol"] or exchange not in (None, "SSE" if code[:2] == "sh" else "SZSE"):
                raise MeozError("incomplete", "订阅股票交易所身份冲突")
        elif exchange in {"SSE", "SZSE"}:
            code = ("sh" if exchange == "SSE" else "sz") + row["symbol"]
        else:
            raise MeozError("incomplete", "订阅股票缺少可核验交易所身份")
        if code.startswith(("sh90", "sz20")):
            continue  # Official mainboard metadata also includes B shares, not this A-share universe.
        if not code.startswith(("sh60", "sz00")):
            raise MeozError("incomplete", "订阅股票市场身份冲突")
        selected.add(code)
    if not selected:
        raise MeozError("incomplete", "主板非ST订阅名单为空")
    return sorted(selected)


def ticket_url(ticket, selected_count):
    limit = ticket.get("tick_sub_limit")
    if (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
            or selected_count > limit or ticket.get("stream_version") != "tick_v1"):
        raise MeozError("no_permission", "订阅槽位不足或协议版本不匹配")
    try:
        parsed = urlsplit(str(ticket.get("url", "")))
        port = parsed.port
    except ValueError:
        raise MeozError("incomplete", "订阅票据地址格式无效") from None
    if (parsed.scheme != "wss" or parsed.hostname not in {"meoz.cn", "t.meoz.cn", "sz.meoz.cn", "sh.meoz.cn"}
            or port not in (None, 443) or parsed.username or parsed.password
            or parsed.path != "/api/tick_stream_v1"):
        raise MeozError("incomplete", "订阅票据地址不是官方WSS入口")
    return ticket["url"], urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), limit


def subscription_ack(message, symbols):
    """Accept a positive subscription acknowledgement, never infer it from ticks."""
    fields = message.get("fields")
    if fields is None:
        return None
    if (not isinstance(fields, list) or not all(isinstance(f, str) for f in fields) or len(fields) != len(set(fields)) or not set(FIELDS) <= set(fields)):
        raise MeozError("incomplete", "订阅确认字段缺失或重复")
    names = message.get("symbols")
    count = message.get("subscribed_count", message.get("subscribed", message.get("count", message.get("n"))))
    if isinstance(names, list):
        try:
            actual = {instrument(s)["code"] for s in names}
        except (ValueError, TypeError, AttributeError):
            raise MeozError("incomplete", "订阅确认股票无效") from None
        if len(names) != len(actual) or actual != set(symbols):
            raise MeozError("incomplete", "订阅确认股票与请求不一致")
        count = len(names)
    if not isinstance(count, int) or isinstance(count, bool) or count != len(symbols):
        raise MeozError("incomplete", "订阅确认数量与请求不一致")
    return fields


def decode_rows(message, fields, selected, received, cutoff):
    items = message.get("i")
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > 20000:
        raise MeozError("incomplete", "订阅推送数组无效或过大")
    rows = []
    for item in items:
        if not isinstance(item, list) or len(item) != len(fields):
            raise MeozError("incomplete", "订阅推送行与字段不匹配")
        row = dict(zip(fields, item, strict=True))
        try:
            code = instrument(row["symbol"])["code"]
            quoted = timestamp(row["time"])
            sent = timestamp(row["send_ts_ms"]) if row["send_ts_ms"] is not None else None
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError, OSError):
            raise MeozError("incomplete", "订阅推送代码或时间无效") from None
        if (code not in selected or quoted.date() != cutoff.date() or not quoted <= received
                or sent is not None and not quoted <= sent <= received or received > cutoff):
            raise MeozError("incomplete", "订阅推送日期、股票或接收时间冲突")
        for key in ("bidPrice", "askPrice", "bidVol", "askVol"):
            values = row[key]
            if values is not None and (not isinstance(values, list) or len(values) != 5
                    or any(v is not None and ((n := number(v)) is None or n < 0) for v in values)):
                raise MeozError("incomplete", "订阅盘口档位或数值无效")
        if any(row[k] is not None and ((n := number(row[k])) is None or n < 0)
               for k in ("lastPrice", "volume", "amount")):
            raise MeozError("incomplete", "订阅价量额无效")
        rows.append({**row, "code": code, "quoted": quoted, "sent": sent, "received": received})
    return rows


class StreamNormalizer:
    """Pin units from matching REST books and an actual final-auction amount proof."""

    def __init__(self):
        self.book_divisor = None
        self.volume_divisor = None
        self.book_evidence = None
        self.volume_evidence = None

    def certify_books(self, native, witness):
        for side, key in (("bid", "bidPrice"), ("ask", "askPrice")):
            if native[key] is None or any(number(witness.get(side + str(i + 1))) != number(native[key][i])
                                          for i in range(2)):
                raise MeozError("incomplete", "订阅盘口价格与同一时刻HTTP证据不一致")
        raw_pairs = [(native["bidVol"][i], witness.get("bid_vol" + str(i + 1))) for i in range(2)]
        raw_pairs += [(native["askVol"][i], witness.get("ask_vol" + str(i + 1))) for i in range(2)]
        pairs: list[tuple[float, float]] = []
        for a, b in raw_pairs:
            observed, reference = number(a), number(b)
            if observed is not None and reference is not None:
                pairs.append((observed, reference))
        candidates = [d for d in (1, 100) if any(a > 0 and b > 0 for a, b in pairs)
                      and all(math.isclose(a / d, b, rel_tol=1e-6, abs_tol=1e-6) for a, b in pairs)]
        if len(candidates) != 1:
            raise MeozError("incomplete", "订阅盘口单位未能与同一时刻HTTP盘口核验")
        if self.book_divisor is not None and self.book_divisor != candidates[0]:
            raise MeozError("incomplete", "订阅盘口单位发生变化")
        self.book_divisor = candidates[0]
        self.book_evidence = {"method": "same-timestamp-rest-book", "symbol": native["symbol"],
                              "asOf": native["quoted"].isoformat(), "divisor": self.book_divisor}

    def normalize(self, row, source):
        if self.book_divisor is None:
            raise MeozError("incomplete", "订阅盘口单位尚未核验")
        volume, amount, price = (number(row[k]) for k in ("volume", "amount", "lastPrice"))
        if self.volume_divisor is None and row["quoted"].strftime("%H%M") == "0925" and volume and amount and price:
            divisors = [d for d in (1, 100) if math.isclose(volume / d * 100 * price, amount, rel_tol=.002, abs_tol=1)]
            if len(divisors) == 1:
                self.volume_divisor = divisors[0]
                self.volume_evidence = {"method": "final-auction-price-volume-amount", "symbol": row["symbol"],
                                        "asOf": row["quoted"].isoformat(), "divisor": self.volume_divisor}
        raw = {"symbol": row["code"][2:], "tradedate": row["quoted"].strftime("%Y%m%d"),
               "time": row["quoted"].isoformat(), "close": price,
               "vol": 0 if volume == 0 else volume / self.volume_divisor if volume is not None and self.volume_divisor else None,
               "amount": amount, "transaction_num": None}
        for side, prices, quantities in (("bid", "bidPrice", "bidVol"), ("ask", "askPrice", "askVol")):
            for i in range(2):
                raw[side + str(i + 1)] = (row[prices] or [None] * 5)[i]
                v = number((row[quantities] or [None] * 5)[i])
                raw[side + "_vol" + str(i + 1)] = None if v is None else v / self.book_divisor
        return {"code": row["code"], "raw": raw, "asOf": row["quoted"].isoformat(),
                "availableAt": row["received"].isoformat(),
                "sendAt": row["sent"].isoformat() if row["sent"] is not None else None,
                "source": source, "transport": "websocket", "volumeUnit": "lot", "bookVolumeUnit": "lot",
                "unitEvidence": {"book": self.book_evidence, "volume": self.volume_evidence}}


async def _worker(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A cancelled subscription cannot close a client used by its bounded worker.
        try:
            await task
        except Exception:
            pass
        raise


async def _status(callback, **values):
    if callback is not None:
        result = callback(values)
        if inspect.isawaitable(result):
            await result


async def stream_records(provider, symbols, *, deadline, on_status=None, connector=connect, sleep=asyncio.sleep):
    """One connection at a time, bounded retries; receive and send timestamps never backdated."""
    cutoff = timestamp(deadline)
    symbols = sorted(set(symbols))
    if not symbols:
        raise MeozError("incomplete", "订阅股票名单为空")
    normalizer, seen, pending = StreamNormalizer(), {}, []
    attempts = 0
    last_witness = -float("inf")
    while provider.clock() < cutoff and attempts < 5:
        attempts += 1
        try:
            # This bounded synchronous request must finish before its source client is released.
            ticket = await _worker(provider.stream_ticket, symbols, deadline=cutoff)
            uri, source, limit = ticket_url(ticket, len(symbols))
            await _status(on_status, status="connecting", selectedCount=len(symbols), slotLimit=limit, attempts=attempts)
            proxy = (provider.settings.get("httpProxy") or None) if (provider.settings.get("httpProxyEnabled")
                     and not provider.settings.get("forceNoProxyForFetch", True)) else None
            remaining = max(.01, min(10, (cutoff - provider.clock()).total_seconds()))
            async with connector(uri, proxy=proxy, open_timeout=remaining, close_timeout=2,
                                 max_size=8 * 1024 * 1024, max_queue=8, logger=_LOGGER) as socket:
                fields = None
                ack_until = provider.clock() + timedelta(seconds=10)
                last_health = provider.monotonic()
                while provider.clock() < cutoff:
                    if fields is None and provider.clock() >= ack_until:
                        raise MeozError("incomplete", "订阅确认超时")
                    try:
                        payload = await asyncio.wait_for(socket.recv(), min(1, max(.01, (cutoff - provider.clock()).total_seconds())))
                    except TimeoutError:
                        yield []  # Allow the source to flush pending records even without a new frame.
                        continue
                    received = provider.clock()
                    if received > cutoff:
                        break
                    try:
                        message = json.loads(payload)
                    except (ValueError, TypeError):
                        raise MeozError("incomplete", "订阅消息不是JSON") from None
                    if not isinstance(message, dict):
                        raise MeozError("incomplete", "订阅消息结构无效")
                    if message.get("error") or message.get("type") == "error":
                        raise MeozError("no_permission", "服务器拒绝订阅请求")
                    if message.get("fields") is not None:
                        fields = subscription_ack(message, symbols)
                        await _status(on_status, status="subscribed", selectedCount=len(symbols),
                                      confirmedCount=len(symbols), slotLimit=limit, fields=fields, attempts=attempts)
                    if message.get("i") is not None and fields is None:
                        raise MeozError("incomplete", "收到行情但尚未确认完整订阅")
                    rows = decode_rows(message, fields or [], set(symbols), received, cutoff)
                    for row in rows:
                        prior = seen.get(row["code"])
                        quoted = row["quoted"].isoformat()
                        seen[row["code"]] = {"count": (prior or {}).get("count", 0) + 1,
                            "firstAsOf": (prior or {}).get("firstAsOf", quoted), "lastAsOf": quoted,
                            "maxGapSeconds": max((prior or {}).get("maxGapSeconds", 0),
                                max(0, (row["quoted"] - timestamp(prior["lastAsOf"])).total_seconds()) if prior else 0)}
                    pending.extend(rows)
                    if normalizer.book_divisor is None and pending and provider.monotonic() - last_witness >= 3:
                        last_witness = provider.monotonic()
                        candidate = next((r for r in reversed(pending) if r["bidVol"] and r["askVol"]
                                          and any(number(v, 0) > 0 for v in r["bidVol"][:2] + r["askVol"][:2])), None)
                        if candidate is not None:
                            quoted = candidate["quoted"]
                            witness = await _worker(provider.tick_history, quoted.strftime("%Y%m%d"),
                                [candidate["code"]], start_time=quoted.strftime("%H:%M:%S.%f")[:-3],
                                end_time=(quoted + timedelta(milliseconds=1)).strftime("%H:%M:%S.%f")[:-3],
                                deadline=cutoff, budget_seconds=3)
                            exact = [r for r in witness["rows"] if r.get("symbol") == candidate["code"][2:]
                                     and timestamp(r["time"]) == quoted]
                            if len(exact) == 1:
                                normalizer.certify_books(candidate, exact[0])
                    if len(pending) > max(1000, len(symbols) * 5):
                        raise MeozError("incomplete", "订阅单位未核验且暂存上限已到")
                    if normalizer.book_divisor is not None:
                        records = [normalizer.normalize(row, source) for row in pending]
                        pending.clear()
                        yield records
                    if provider.monotonic() - last_health >= 10:
                        last_health = provider.monotonic()
                        await _status(on_status, status="receiving", selectedCount=len(symbols),
                            confirmedCount=len(symbols), slotLimit=limit, attempts=attempts, receivedCount=sum(r["count"] for r in seen.values()),
                            stockCount=len(seen), stockHealth=seen, bookDivisor=normalizer.book_divisor,
                            volumeDivisor=normalizer.volume_divisor,
                            counts={"receivedRecords": sum(r["count"] for r in seen.values()), "receivedSymbols": len(seen)})
            break
        except asyncio.CancelledError:
            raise
        except (MeozError, OSError, TimeoutError, WebSocketException) as error:
            status = error.status if isinstance(error, MeozError) else "incomplete"
            await _status(on_status, status="failed" if status == "no_permission" or attempts == 5 else "retrying",
                          selectedCount=len(symbols), attempts=attempts, message=str(error) if isinstance(error, MeozError) else "订阅连接中断")
            if status == "no_permission" or attempts == 5:
                raise MeozError(status, "订阅无法继续，等待真实Tick回补") from None
            await sleep(min(2 ** attempts, 15, max(0, (cutoff - provider.clock()).total_seconds())))
    await _status(on_status, status="complete", selectedCount=len(symbols), attempts=attempts,
                  receivedCount=sum(r["count"] for r in seen.values()), stockCount=len(seen), stockHealth=seen,
                  bookDivisor=normalizer.book_divisor, volumeDivisor=normalizer.volume_divisor,
                  counts={"receivedRecords": sum(r["count"] for r in seen.values()), "receivedSymbols": len(seen)})
