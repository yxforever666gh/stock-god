"""Prediction settings retain stored ownership and use compare-and-swap revisions."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .storage.db import Database


class SettingsConflict(ValueError):
    pass


DEFAULTS = {
    "tushareToken": "",
    "meozApiKey": "",
    "crawlTimeOut": 60,
    "kDays": 60,
    "browserPath": "",
    "browserPoolSize": 1,
    "httpProxy": "",
    "httpProxyEnabled": False,
    "forceNoProxyForFetch": True,
    "qgqpBId": "",
    "experimentalEvidenceEnabled": False,
    "minuteProviderMode": "public",
    "minuteProviderOrder": ["tencent", "sina", "akshare", "private"],
    "minuteLongHistoryHintEnabled": True,
    "privateMinuteEnabled": False,
    "privateMinuteBaseUrl": "",
    "privateMinuteApiKey": "",
    "privateMinuteTimeoutSec": 60,
    "privateMinuteMinIntervalMs": 0,
    "privateMinuteProxyMode": "disable",
    "privateMinuteLevel": "1min",
    "akshareEnabled": True,
    "sinaMinuteEnabled": True,
    "tencentMinuteEnabled": True,
    "eastmoneyMinuteEnabled": True,
    "akshareMinuteSourceMode": "auto",
    "predictionAutoEnabled": True,
    "predictionEmailEnabled": False,
    "predictionEmailTo": "",
    "predictionEmailFrom": "",
    "predictionEmailSmtpHost": "",
    "predictionEmailSmtpPort": 0,
    "predictionEmailSmtpUsername": "",
    "predictionEmailSmtpPassword": "",
    "predictionEmailSlots": [],
}

MODEL_FIELDS = {
    "name": "name",
    "baseUrl": "base_url",
    "apiKey": "api_key",
    "modelName": "model_name",
    "apiProtocol": "api_protocol",
    "maxTokens": "max_tokens",
    "temperature": "temperature",
    "timeOut": "time_out",
    "httpProxy": "http_proxy",
    "httpProxyEnabled": "http_proxy_enabled",
    "sort": "sort",
    "disabled": "disabled",
}
GLOBAL_FIELDS = {
    "darkTheme": "dark_theme",
    "refreshInterval": "refresh_interval",
    "updateBasicInfoOnStart": "update_basic_info_on_start",
}
BOOLEAN_FIELDS = {key for key, value in DEFAULTS.items() if isinstance(value, bool)}
INTEGER_FIELDS = {
    key for key, value in DEFAULTS.items() if isinstance(value, int) and not isinstance(value, bool)
}


@dataclass(frozen=True)
class SettingsSnapshot:
    config: dict
    models: list[dict]
    revision: int

    def payload(self) -> dict:
        return {
            "revision": self.revision,
            "config": copy.deepcopy(self.config),
            "aiConfigs": copy.deepcopy(self.models),
        }


def _public_key(key: str) -> str:
    return key.replace("research2", "prediction", 1) if key.startswith("research2") else key


def _stored_key(key: str) -> str:
    return key.replace("prediction", "research2", 1) if key.startswith("prediction") else key


def _model(row) -> dict:
    source = dict(row)
    result = {name: source.get(column) for name, column in MODEL_FIELDS.items()}
    result.update(ID=source["id"], CreatedAt=source.get("created_at"), UpdatedAt=source.get("updated_at"))
    result["disabled"] = bool(result["disabled"])
    result["httpProxyEnabled"] = bool(result["httpProxyEnabled"])
    result["timeOut"] = result["timeOut"] or 300
    if result["apiProtocol"] not in {"openai_responses", "anthropic_messages"}:
        result["apiProtocol"] = "chat_completions"
    return result


def _normalize(config: dict) -> dict:
    if not isinstance(config, dict) or any(
        key not in DEFAULTS or value is None for key, value in config.items()
    ):
        raise ValueError("invalid prediction configuration field")
    result = copy.deepcopy(DEFAULTS)
    result.update(copy.deepcopy(config))
    for key in BOOLEAN_FIELDS:
        if not isinstance(result[key], bool):
            raise ValueError(f"{key} must be boolean")
    for key in INTEGER_FIELDS:
        if not isinstance(result[key], int) or isinstance(result[key], bool):
            raise ValueError(f"{key} must be integer")
    for key, default in DEFAULTS.items():
        if isinstance(default, str) and not isinstance(result[key], str):
            raise ValueError(f"{key} must be a string")
    order = result["minuteProviderOrder"]
    if not isinstance(order, list) or any(not isinstance(value, str) for value in order):
        raise ValueError("minuteProviderOrder must be a string array")
    if not order:
        order = (
            ["private", "tencent", "sina", "akshare"]
            if result["minuteProviderMode"] == "private"
            else DEFAULTS["minuteProviderOrder"]
        )
    normalized = []
    for raw in order:
        value = raw.strip().lower()
        if not value:
            continue
        if value not in DEFAULTS["minuteProviderOrder"]:
            raise ValueError(f"未知分钟数据源：{raw}")
        if value not in normalized:
            normalized.append(value)
    result["minuteProviderOrder"] = normalized + [
        p for p in DEFAULTS["minuteProviderOrder"] if p not in normalized
    ]
    if result["crawlTimeOut"] <= 0:
        result["crawlTimeOut"] = 60
    if result["kDays"] < 30:
        result["kDays"] = 60
    if result["privateMinuteTimeoutSec"] <= 0:
        result["privateMinuteTimeoutSec"] = 60
    if result["privateMinuteMinIntervalMs"] < 0:
        raise ValueError("分钟来源间隔不能为负数")
    result["privateMinuteBaseUrl"] = result["privateMinuteBaseUrl"].strip()
    result["privateMinuteApiKey"] = result["privateMinuteApiKey"].strip()
    result["meozApiKey"] = result["meozApiKey"].strip()
    if result["privateMinuteEnabled"] and not (
        result["privateMinuteBaseUrl"] and result["privateMinuteApiKey"]
    ):
        raise ValueError("已启用的私人分钟来源需要 URL 和 API Key")
    if not any(result[key] for key in ("akshareEnabled", "sinaMinuteEnabled", "tencentMinuteEnabled")):
        if not (result["privateMinuteEnabled"] and result["privateMinuteLevel"] == "1min"):
            raise ValueError("至少启用一个分钟图来源")
    valid_slots = {f"{minute // 60:02d}:{minute % 60:02d}" for minute in range(570, 690, 5)}
    valid_slots.add("base43")
    slots = result["predictionEmailSlots"]
    if not isinstance(slots, list) or any(slot not in valid_slots for slot in slots):
        raise ValueError("邮件时间段无效")
    result["predictionEmailSlots"] = sorted(set(slots))
    if result["predictionEmailEnabled"]:
        if not slots:
            raise ValueError("开启自动邮件时，请至少选择一个时间段")
        for key in ("predictionEmailTo", "predictionEmailFrom", "predictionEmailSmtpHost"):
            if not result[key].strip() or "\r" in result[key] or "\n" in result[key]:
                raise ValueError("邮件配置不完整或无效")
        if not 1 <= result["predictionEmailSmtpPort"] <= 65535:
            raise ValueError("SMTP 端口无效")
    return result


class SettingsStore:
    def __init__(self, database: Database):
        self.database = database

    def initialize(self):
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO settings(dark_theme,refresh_interval,update_basic_info_on_start,enable_news) "
                "SELECT 0,1,0,0 WHERE NOT EXISTS(SELECT 1 FROM settings WHERE deleted_at IS NULL)"
            )

    def _load(self, connection) -> SettingsSnapshot:
        record = connection.execute(
            "SELECT config_json,revision FROM research_settings WHERE center='research2'"
        ).fetchone()
        if record is None:
            raise ValueError("prediction configuration is missing; upgrade the database first")
        values = {_public_key(key): value for key, value in json.loads(record["config_json"]).items()}
        config = copy.deepcopy(DEFAULTS)
        config.update({key: value for key, value in values.items() if key in DEFAULTS and value is not None})
        models = [
            _model(row)
            for row in connection.execute(
                "SELECT * FROM ai_config WHERE owner='research2' AND archived_at IS NULL "
                "ORDER BY CASE WHEN sort<=0 THEN id ELSE sort END,id"
            )
        ]
        return SettingsSnapshot(config=config, models=models, revision=int(record["revision"]))

    def load(self) -> SettingsSnapshot:
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            try:
                return self._load(connection)
            finally:
                connection.rollback()

    def save(self, revision: int, config: dict, models: list[dict]) -> SettingsSnapshot:
        if not isinstance(revision, int) or revision <= 0 or not isinstance(models, list):
            raise ValueError("invalid prediction settings payload")
        normalized = _normalize(config)
        draft = copy.deepcopy(models)
        for model in draft:
            if not isinstance(model, dict):
                raise ValueError("empty AI model")
            ident = model.get("ID", model.get("id", 0))
            if not isinstance(ident, int) or ident < 0:
                raise ValueError("AI model ID must be a nonnegative integer")
            model["ID"] = ident
            model["timeOut"] = model.get("timeOut") or 300
            if model.get("apiProtocol") not in {"openai_responses", "anthropic_messages"}:
                model["apiProtocol"] = "chat_completions"
        raw = json.dumps(
            {_stored_key(k): v for k, v in normalized.items()}, ensure_ascii=False, sort_keys=True
        )
        now = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            result = connection.execute(
                "UPDATE research_settings SET config_json=?,revision=revision+1 WHERE center='research2' AND revision=?",
                (raw, revision),
            )
            if result.rowcount != 1:
                raise SettingsConflict("prediction configuration revision conflict")
            existing = {
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM ai_config WHERE owner='research2' AND archived_at IS NULL"
                )
            }
            seen = set()
            for model in draft:
                ident = model["ID"]
                if ident and (ident not in existing or ident in seen):
                    raise ValueError("AI model does not belong to this configuration")
                if ident:
                    seen.add(ident)
            for ident in existing - seen:
                connection.execute(
                    "UPDATE ai_config SET archived_at=? WHERE id=? AND owner='research2'", (now, ident)
                )
            for index, model in enumerate(draft, 1):
                values = {}
                for name, column in MODEL_FIELDS.items():
                    default = (
                        False
                        if name in {"disabled", "httpProxyEnabled"}
                        else (0 if name in {"sort", "maxTokens", "temperature"} else "")
                    )
                    values[column] = model.get(name, default)
                values.update(sort=index, updated_at=now)
                if model["ID"]:
                    assignments = ",".join(f"{key}=?" for key in values)
                    connection.execute(
                        f"UPDATE ai_config SET {assignments} WHERE id=? AND owner='research2'",
                        (*values.values(), model["ID"]),
                    )
                else:
                    values.update(owner="research2", created_at=now)
                    columns = ",".join(values)
                    connection.execute(
                        f"INSERT INTO ai_config ({columns}) VALUES ({','.join('?' for _ in values)})",
                        tuple(values.values()),
                    )
            return self._load(connection)

    def global_values(self) -> dict:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM settings WHERE deleted_at IS NULL ORDER BY id LIMIT 1"
            ).fetchone()
        if row is None:
            raise ValueError("application settings are missing")
        source = dict(row)
        return {
            key: bool(source[column]) if key != "refreshInterval" else source[column]
            for key, column in GLOBAL_FIELDS.items()
        }

    def save_global(self, changes: dict) -> dict:
        if not isinstance(changes, dict) or any(key not in GLOBAL_FIELDS for key in changes):
            raise ValueError("only application display settings belong to global settings")
        for key, value in changes.items():
            if key == "refreshInterval":
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise ValueError("refreshInterval must be positive")
            elif not isinstance(value, bool):
                raise ValueError(f"{key} must be boolean")
        if changes:
            with self.database.transaction() as connection:
                assignments = ",".join(f"{GLOBAL_FIELDS[key]}=?" for key in changes)
                connection.execute(
                    f"UPDATE settings SET {assignments} WHERE id=(SELECT id FROM settings "
                    "WHERE deleted_at IS NULL ORDER BY id LIMIT 1)",
                    tuple(changes.values()),
                )
        return self.global_values()
