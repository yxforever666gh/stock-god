import copy
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from stock_god.audit import AuditStore
from stock_god.prediction.core import SLOTS, local, stamp
from stock_god.prediction.evidence_store import EvidenceStore
from stock_god.prediction.repository import insert
from stock_god.prediction.service import PredictionService
from stock_god.storage.db import Database


class Clock:
    def __init__(self, at="2026-09-24T09:50:10+08:00"):
        self.at = local(at)

    def __call__(self):
        return self.at


class Settings:
    def __init__(self, database):
        self.db = database
        self.config = {
            "predictionAutoEnabled": True,
            "predictionEmailEnabled": False,
            "predictionEmailSlots": [],
            "predictionEmailTo": "user@example.com",
            "predictionEmailFrom": "sender@example.com",
            "predictionEmailSmtpHost": "mail.example.com",
            "predictionEmailSmtpPort": 465,
            "predictionEmailSmtpUsername": "sender@example.com",
            "predictionEmailSmtpPassword": "smtp-secret",
        }
        self.save()

    def load(self):
        return SimpleNamespace(
            config=copy.deepcopy(self.config), models=[{"ID": 1, "modelName": "fixture"}], revision=1
        )

    def save(self):
        with self.db.transaction() as con:
            con.execute(
                "INSERT INTO research_settings(center,config_json,revision) VALUES ('research2',?,1) ON CONFLICT(center) DO UPDATE SET config_json=excluded.config_json",
                (json.dumps(self.config),),
            )


class Market:
    def __init__(self, clock):
        self.clock = clock
        self.count = 7
        self.price = 10.0
        self.fail_quotes = set()
        self.quote_hook = None
        self.history = {}
        self.bar_rows = []
        self.network_calls = 0
        self.settings = []

    def with_settings(self, config):
        self.settings.append(copy.deepcopy(config))
        return self

    def close(self):
        pass

    def is_trading_day(self, at):
        return local(at).weekday() < 5

    def quote(self, stock_code):
        self.network_calls += 1
        if self.quote_hook:
            self.quote_hook(stock_code)
        if stock_code in self.fail_quotes:
            raise ValueError("fixture provider unavailable")
        return {
            "code": stock_code,
            "name": stock_code,
            "price": self.price,
            "preClose": self.price,
            "asOf": stamp(self.clock()),
            "source": "fixture",
            "status": "ok",
        }

    def collect_prediction_evidence(self, cutoff, exclusions, cash):
        self.last_cash = cash
        candidates = [
            {"code": f"sh6000{index:02}", "name": f"股票{index}", "referencePrice": 10.0}
            for index in range(1, self.count + 1)
        ]
        documents = [
            {
                "sourceId": "market",
                "category": "market",
                "sourceName": "市场",
                "content": "市场证据",
                "availableAt": stamp(cutoff),
            }
        ]
        for candidate in candidates:
            documents.append(
                {
                    "sourceId": "quote-" + candidate["code"],
                    "category": "quote",
                    "stockCode": candidate["code"],
                    "content": json.dumps(candidate),
                    "availableAt": stamp(cutoff),
                }
            )
        return {
            "candidates": candidates,
            "documents": documents,
            "cutoffAt": stamp(cutoff),
            "freezeAt": stamp(cutoff),
            "prompt": "frozen fixture",
            "coveragePct": 1.0,
            "degraded": False,
            "evidenceSetId": "fixture-evidence",
            "candidateReferencePrices": {c["code"]: 10.0 for c in candidates},
            "sourceStatusJson": "[]",
        }

    def buy_day_data(self, item):
        key = (item["stockCode"], local(item["buyAt"]).date().isoformat())
        if key not in self.history:
            raise ValueError("missing session")
        return self.history[key]

    def daily_closes(self, code, start, end):
        return [
            {
                "tradingDate": (start + timedelta(days=i)).date().isoformat(),
                "close": 10.0,
                "source": "fixture",
            }
            for i in range((end.date() - start.date()).days + 1)
        ]

    def bars(self, *args, **kwargs):
        self.network_calls += 1
        return self.bar_rows

    def cached_bars(self, *args, **kwargs):
        return self.bar_rows

    def refresh_recommendation_chart(self, code, start, end):
        rows = self.bars(code, start, end, period="1m", adjustment="none")
        quote = self.quote(code)
        dates = set()
        day = start
        while day.date() <= end.date():
            if self.is_trading_day(day):
                dates.add(day.date().isoformat())
            day += timedelta(days=1)
        return {"bars": rows, "quote": quote, "openedDates": dates, "errors": []}


class AI:
    def __init__(self, market):
        self.market = market
        self.calls = []
        self.responses = []
        self.hook = None

    async def complete(self, **kwargs):
        self.calls.append(kwargs["prompt"])
        if self.hook:
            await self.hook()
        attempt = {
            "id": f"attempt-{len(self.calls)}",
            "model": "fixture",
            "providerName": "fixture",
            "status": "success",
        }
        kwargs["on_attempt"](attempt)
        if self.responses:
            content = self.responses.pop(0)
        else:
            rows = []
            for index in range(1, self.market.count + 1):
                stock_code = f"sh6000{index:02}"
                rows.append(
                    {
                        "code": stock_code,
                        "name": "model name",
                        "marketScore": 20,
                        "sectorScore": 10,
                        "stockScore": 40 - index,
                        "catalystScore": 0,
                        "riskDeduction": 0,
                        "finalScore": 70 - index,
                        "referencePrice": 999,
                        "summary": "简述",
                        "quantData": "量化",
                        "freshCatalyst": "无新催化",
                        "oldBackground": "背景",
                        "mainRisk": "风险",
                        "cancelConditions": "条件",
                        "sourceRefs": ["market", "quote-" + stock_code],
                        "scoreReasons": {},
                    }
                )
            content = json.dumps(
                {"tradingDay": True, "conclusion": "测试结论", "recommendations": rows}, ensure_ascii=False
            )
        return SimpleNamespace(
            content=content,
            response_id="fixture",
            model="fixture",
            provider_name="fixture",
            attempts=[attempt],
        )


@pytest.fixture
def env(tmp_path):
    database = Database(tmp_path / "prediction.db")
    with database.connection() as con:
        con.executescript((Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"))
    clock = Clock()
    settings = Settings(database)
    market = Market(clock)
    audit = AuditStore(database)
    ai = AI(market)
    with database.transaction() as con:
        for slot in SLOTS:
            insert(con, "accounts", {"slot": slot, "initial_cash": 10000.0, "cash": 10000.0})
            insert(
                con,
                "account_capital_events",
                {
                    "event_id": "initial-" + slot,
                    "slot": slot,
                    "event_type": "initial_external",
                    "amount": 10000.0,
                    "external": True,
                    "source": "fixture",
                    "effective_at": "2026-08-27T09:00:00+08:00",
                    "trading_date": "2026-08-27",
                },
            )
    service = PredictionService(
        database,
        market,
        settings,
        lambda configs: ai,
        audit,
        clock=clock,
        mailer=lambda config, delivery: None,
        evidence_store=EvidenceStore(database, clock=clock),
    )
    return SimpleNamespace(
        db=database, clock=clock, settings=settings, market=market, audit=audit, ai=ai, service=service
    )
