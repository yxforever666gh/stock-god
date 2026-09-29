"""Freeze-aware source validation and server-owned report rendering."""

from __future__ import annotations

import json
import math
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from .core import PredictionError, code, json_text, local, lot_size, parse_time, positive, stamp, trade_cost

PROMPT = (Path(__file__).parent / "prompts" / "overnight_strength.md").read_text(encoding="utf-8")


def payload(document):
    value = document.get("content", "")
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def objects(value):
    if isinstance(value, dict):
        yield value
        for key in sorted(value):
            yield from objects(value[key])
    elif isinstance(value, list):
        for item in value:
            yield from objects(item)


def empty(value):
    if isinstance(value, dict):
        if value.get("status") == "empty":
            return True
        for key in ("data", "items", "rows", "result"):
            if key in value:
                return empty(value[key])
    return value in (None, "", [], {})


def usable(document, freeze):
    available = parse_time(document.get("availableAt"))
    value = payload(document)
    if document.get("error") or not available or available > freeze or empty(value):
        return False
    if isinstance(value, dict):
        if value.get("success") is False:
            return False
        if value.get("status") and str(value["status"]).lower() not in ("ok", "partial", "frozen"):
            return False
    return True


def normalized(value):
    try:
        return code(value)
    except PredictionError:
        return ""


def owns(row, stock_code):
    return (
        any(
            normalized(row.get(key)) == stock_code
            for key in ("SECURITY_CODE", "SECUCODE", "stockCode", "stock_code", "code")
        )
        or row.get("entityId") == "stock:" + stock_code
    )


def stock_applies(document, stock_code):
    category = document.get("category", "")
    if category not in ("stock", "quote", "minute") and not category.startswith("stock_"):
        return True
    if document.get("stockCode"):
        return normalized(document["stockCode"]) == stock_code
    text = " ".join(str(document.get(key, "")) for key in ("sourceId", "sourceName", "content")).lower()
    return stock_code in text or stock_code[-6:] in text


def theme_members(documents, stock_code, freeze):
    result = {}
    for doc in documents:
        value = payload(doc)
        if doc.get("category") != "theme" or not usable(doc, freeze) or not isinstance(value, dict):
            continue
        if any(
            isinstance(row, dict) and row.get("assetType") == "stock" and owns(row, stock_code)
            for row in value.get("stockConstituents", [])
        ) and value.get("themeId"):
            result[value["themeId"]] = doc["sourceId"]
    return result


def applies(document, stock_code, documents, freeze):
    if document.get("category") not in ("theme", "catalyst"):
        return stock_applies(document, stock_code)
    value = payload(document)
    return isinstance(value, dict) and (
        owns(value, stock_code) or value.get("themeId") in theme_members(documents, stock_code, freeze)
    )


def catalyst_document(doc):
    return doc.get("category") == "catalyst" or any(
        label in doc.get("sourceName", "") for label in ("公告", "新闻", "互动")
    )


def catalyst_facts(doc, stock_code, cutoff, fresh_since):
    value = payload(doc)
    if isinstance(value, dict) and value.get("themeId") and "event" in value:
        value = value["event"]
    result = []
    for row in objects(value):
        if any(
            normalized(row.get(key)) not in ("", stock_code)
            for key in ("stockCode", "stock_code", "SECURITY_CODE", "SECUCODE")
        ):
            continue
        title = next(
            (
                row[k]
                for k in ("title", "Title", "mainContent")
                if isinstance(row.get(k), str) and row[k].strip()
            ),
            "",
        )
        summary = next(
            (
                row[k]
                for k in ("attachedContent", "summary", "content", "Content")
                if isinstance(row.get(k), str) and row[k].strip()
            ),
            "",
        )
        if not title and not summary:
            continue
        keys = (
            ("attachedPubDate",)
            if "互动" in doc.get("sourceName", "")
            else (
                "eventAt",
                "dataTime",
                "notice_date",
                "NOTICE_DATE",
                "publishedAt",
                "publishTime",
                "PUBLISH_DATE",
            )
        )
        field = next((k for k in keys if row.get(k)), "")
        raw = row.get(field, "")
        at = parse_time(raw)
        relation = "time_unverified"
        if at and fresh_since:
            relation = (
                "after_snapshot"
                if at > cutoff
                else "old_background"
                if at < fresh_since
                else "fresh_available"
            )
        result.append(
            {
                "title": title[:160],
                "summary": summary[:160],
                "eventTime": raw,
                "timeField": field,
                "relation": relation,
            }
        )
    return result


def score_evidence(stock_code, documents, cutoff, freeze, fresh_since):
    result: dict[str, Any] = {
        "sectorState": "membership_unverified",
        "sector": [],
        "catalystState": "source_missing",
        "catalyst": [],
        "unavailableSourceIds": [],
    }
    members = set()
    themes = theme_members(documents, stock_code, freeze)
    for doc in documents:
        if not usable(doc, freeze):
            continue
        facts = []
        for row in objects(payload(doc)):
            if row.get("BOARD_NAME") and owns(row, stock_code):
                facts.append(
                    {
                        key: row[key]
                        for key in (
                            "SECURITY_CODE",
                            "SECUCODE",
                            "BOARD_NAME",
                            "NEW_BOARD_CODE",
                            "BOARD_YIELD",
                            "BOARD_RANK",
                        )
                        if key in row
                    }
                )
                members.update(str(row.get(k)) for k in ("BOARD_NAME", "NEW_BOARD_CODE") if row.get(k))
                result["sectorState"] = (
                    "available" if isinstance(row.get("BOARD_YIELD"), (int, float)) else "membership_only"
                )
        if facts:
            result["sector"].append(
                {"sourceId": doc["sourceId"], "relation": "verified_membership", "facts": facts}
            )
        if doc["sourceId"] in themes.values():
            result["sectorState"] = "available"
            result["sector"].append({"sourceId": doc["sourceId"], "relation": "verified_theme_constituent"})
    priorities = {"fresh_available": 4, "time_unverified": 3, "old_background": 2, "after_snapshot": 1}
    for doc in documents:
        value = payload(doc)
        if usable(doc, freeze) and doc.get("category") == "sector":
            facts = [
                row
                for row in objects(value)
                if any(str(row.get(key, "")) in members for key in ("bd_code", "bd_name", "code", "name"))
            ]
            if facts:
                result["sectorState"] = "available"
                result["sector"].append(
                    {"sourceId": doc["sourceId"], "relation": "exact_board_match", "facts": facts[:8]}
                )
        if not catalyst_document(doc):
            continue
        category = doc.get("category", "")
        scoped = (
            category in ("stock", "quote", "minute") or category.startswith("stock_")
        ) and stock_applies(doc, stock_code)
        theme_applies = isinstance(value, dict) and value.get("themeId") in themes
        direct_applies = category == "catalyst" and isinstance(value, dict) and owns(value, stock_code)
        if not (scoped or theme_applies or direct_applies):
            continue
        if not usable(doc, freeze):
            available = parse_time(doc.get("availableAt"))
            state = (
                "source_unavailable"
                if doc.get("error")
                else "source_time_unverified"
                if not available
                else "source_after_freeze"
                if available > freeze
                else "no_fresh_catalyst"
            )
            if state == "no_fresh_catalyst":
                result["catalyst"].append({"sourceId": doc["sourceId"], "relation": state, "facts": []})
            else:
                result["unavailableSourceIds"].append(doc["sourceId"])
            if result["catalystState"] == "source_missing":
                result["catalystState"] = state
            continue
        facts = sorted(
            catalyst_facts(doc, stock_code, cutoff, fresh_since), key=lambda row: -priorities[row["relation"]]
        )
        state = facts[0]["relation"] if facts else "time_unverified"
        result["catalyst"].append(
            {
                "sourceId": doc["sourceId"],
                "relation": state,
                "facts": facts[:8],
                "omittedFacts": max(0, len(facts) - 8),
            }
        )
        current = result["catalystState"]
        if (
            current.startswith("source_")
            or current == "no_fresh_catalyst"
            or priorities.get(state, 0) > priorities.get(current, 0)
        ):
            result["catalystState"] = state
    return result


def prepare(evidence, market):
    evidence = dict(evidence)
    cutoff = local(evidence["cutoffAt"])
    freeze = local(evidence.get("freezeAt") or evidence["cutoffAt"])
    fresh_since = None
    for n in range(1, 371):
        day = cutoff - timedelta(days=n)
        try:
            opened = market.is_trading_day(day)
        except (OSError, ValueError, RuntimeError):
            break
        if opened:
            fresh_since = day.replace(hour=15, minute=0, second=0, microsecond=0)
            break
    evidence["freezeAt"] = stamp(freeze)
    evidence["catalystWindowStartAt"] = stamp(fresh_since)
    evidence["scoreEvidence"] = {
        normalized(c["code"]): score_evidence(
            normalized(c["code"]), evidence.get("documents", []), cutoff, freeze, fresh_since
        )
        for c in evidence.get("candidates", [])
        if normalized(c["code"])
    }
    return evidence


def build_prompt(evidence):
    # The durable documents include the entire market universe. Only the provider's
    # compact snapshot belongs in model input; the full documents remain in audit.
    parameters = {
        key: evidence.get(key)
        for key in (
            "windowStartAt",
            "windowEndAt",
            "cutoffAt",
            "freezeAt",
            "catalystWindowStartAt",
            "evidenceProfileVersion",
            "coveragePct",
            "degraded",
        )
    }
    return "\n".join(
        (
            PROMPT,
            "\n# 本次执行参数",
            json_text(parameters),
            "\n# 系统注入的紧凑结构化证据",
            str(evidence.get("prompt") or "").strip(),
            "\n# 本轮候选评分依据（按规范化股票代码索引）",
            json_text(evidence.get("scoreEvidence", {})),
            "\n# 输出约束",
            "逐只覆盖冻结候选，包含低分股票；只能引用本轮适用的sourceId。市场20、板块30、个股40、催化10、风险扣分25。",
            (
                '只输出JSON对象：{"tradingDay":true,"conclusion":"结论","recommendations":[{"code":"sh600000","name":"名称",'
                '"marketScore":0,"sectorScore":0,"stockScore":0,"catalystScore":0,"riskDeduction":0,"finalScore":0,"referencePrice":0,'
                '"summary":"","quantData":"","freshCatalyst":"","oldBackground":"","mainRisk":"","cancelConditions":"",'
                '"sourceRefs":["source-id"],"scoreReasons":{"market":"","sector":"","stock":"","catalyst":"","risk":""}}]}'
            ),
        )
    )


def parse_output(content):
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        raise PredictionError("缺少JSON对象")
    value = json.loads(
        content[start : end + 1],
        parse_constant=lambda value: (_ for _ in ()).throw(PredictionError("非有限数值")),
    )
    if not isinstance(value, dict) or not isinstance(value.get("recommendations", []), list):
        raise PredictionError("推荐结果结构无效")
    if value.get("conclusion") is not None and not isinstance(value["conclusion"], str):
        raise PredictionError("conclusion必须是字符串")
    for recommendation in value.get("recommendations", []):
        if not isinstance(recommendation, dict):
            raise PredictionError("推荐项必须为JSON对象")
        for key in (
            "marketScore",
            "sectorScore",
            "stockScore",
            "catalystScore",
            "riskDeduction",
            "finalScore",
            "referencePrice",
        ):
            number = recommendation.get(key)
            if number is None:
                recommendation[key] = 0
            elif (
                isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number)
            ):
                raise PredictionError(key + "必须是有限数字")
        for key in (
            "code",
            "name",
            "summary",
            "quantData",
            "freshCatalyst",
            "oldBackground",
            "mainRisk",
            "cancelConditions",
        ):
            if recommendation.get(key) is None:
                recommendation[key] = ""
            elif not isinstance(recommendation[key], str):
                raise PredictionError(key + "必须是字符串")
        reasons = recommendation.get("scoreReasons")
        if reasons is not None and (
            not isinstance(reasons, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in reasons.items())
        ):
            raise PredictionError("scoreReasons必须是分项文字说明")
        refs = recommendation.get("sourceRefs")
        if refs is not None and (not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs)):
            raise PredictionError("sourceRefs必须是来源ID字符串数组")
    return value


def validate(run_id, evidence, output):
    allowed = {normalized(c["code"]): c for c in evidence.get("candidates", []) if normalized(c["code"])}
    documents = evidence.get("documents", [])
    by_id = {d["sourceId"]: d for d in documents}
    freeze = local(evidence["freezeAt"])
    seen = set()
    items = []
    warnings = []
    prices = evidence.get("candidateReferencePrices")
    for value in output.get("recommendations", []):
        if not isinstance(value, dict):
            warnings.append("推荐必须为对象")
            continue
        stock_code = normalized(value.get("code"))
        issues = []
        if stock_code not in allowed or not stock_code.startswith(("sh60", "sz00")):
            warnings.append(f"{stock_code}不在本次主板冻结候选集合中")
            continue
        if stock_code in seen:
            warnings.append(f"{stock_code}重复评分")
            continue
        seen.add(stock_code)
        refs = value.get("sourceRefs", [])
        if not isinstance(refs, list) or not refs:
            issues.append("未提供sourceRefs")
            refs = []
        support = set()
        for ref in refs:
            doc = by_id.get(ref) if isinstance(ref, str) else None
            if not doc or not usable(doc, freeze) or not applies(doc, stock_code, documents, freeze):
                issues.append("引用无效/过期/属于其他股票的来源" + str(ref))
                continue
            category = doc.get("category", "")
            sid = doc["sourceId"]
            if category == "market" or sid.startswith(("research2:market:", "prediction:market:")):
                support.add("market")
            if category in ("stock", "quote", "minute") or sid.startswith(
                ("research2:quote:", "research2:minutes:", "prediction:quote:", "prediction:minutes:")
            ):
                support.add("stock")
        proof = evidence["scoreEvidence"][stock_code]
        if any(
            link["sourceId"] in refs and link["relation"] == "fresh_available" for link in proof["catalyst"]
        ):
            support.add("catalyst")
        scores = {}
        for field, maximum in [
            ("marketScore", 20),
            ("sectorScore", 30),
            ("stockScore", 40),
            ("catalystScore", 10),
            ("riskDeduction", 25),
        ]:
            score = value.get(field, 0)
            if not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= maximum:
                issues.append(field + "超出范围")
                score = 0
            scores[field] = score
        for dimension in ("market", "stock", "catalyst"):
            if scores[dimension + "Score"] > 0 and dimension not in support:
                issues.append(dimension + "评分缺少对应可用来源")
        price = (
            prices.get(stock_code)
            if prices is not None
            else allowed[stock_code].get("referencePrice", value.get("referencePrice"))
        )
        if not positive(price):
            issues.append("缺少截止点证据参考价")
        if issues:
            warnings.extend(stock_code + message for message in issues)
            continue
        final = (
            sum(scores[field] for field in ("marketScore", "sectorScore", "stockScore", "catalystScore"))
            - scores["riskDeduction"]
        )
        if abs(value.get("finalScore", 0) - final) > 0.01:
            warnings.append(stock_code + "分项加总与最终分不一致，已按分项重新计算")
        row = {
            "recommendation_id": str(uuid.uuid4()),
            "analysis_run_id": run_id,
            "stock_code": stock_code,
            "stock_name": allowed[stock_code].get("name") or value.get("name", ""),
            "final_score": final,
            "reference_price": price,
            "estimated_lot_cost": math.floor(
                -trade_cost(stock_code, price, lot_size(stock_code))["net_cash_flow"] * 100 + 0.5
            )
            / 100,
            "source_refs": "\n".join(dict.fromkeys(refs)),
            "selection_role": "",
            "buy_lower": 0,
            "buy_upper": 0,
        }
        for field in (
            "marketScore",
            "sectorScore",
            "stockScore",
            "catalystScore",
            "riskDeduction",
            "summary",
            "quantData",
            "freshCatalyst",
            "oldBackground",
            "mainRisk",
            "cancelConditions",
        ):
            import re

            row[re.sub(r"([A-Z])", lambda m: "_" + m[1].lower(), field)] = scores.get(
                field, value.get(field, "")
            )
        items.append(row)
    warnings.extend(
        stock_code + "缺少评分，必须覆盖全部冻结候选" for stock_code in allowed if stock_code not in seen
    )
    items.sort(key=lambda row: (-row["final_score"], row["stock_code"]))
    for rank, row in enumerate(items, 1):
        row["selection_rank"] = rank
    return items, warnings


def render_report(run, items, evidence, output, warnings):
    def cell(value):
        return str(value or "--").replace("|", "\\|").replace("\n", " ")

    lines = [
        "# 股票预测 隔日强势筛选",
        "",
        "## 分析结论",
        "",
        f"- 交易日：{run['trading_date']}",
        f"- 推荐数量：{len(items)}",
        f"- 当日尝试：第{run['attempt_no']}次",
        f"- 计划区间：{run['scheduled_slot']}",
        f"- 启动所属区间：{run['slot'] or '--'}",
        f"- 实际启动：{run['started_at']}",
        f"- 报告生成：{run['generated_at']}",
        f"- 市场快照时点：{run['evidence_cutoff_at']}",
        f"- 证据冻结时间：{evidence['freezeAt']}",
        f"- 证据覆盖：{evidence.get('coveragePct', 0)}",
        f"- 证据质量：{'降级' if evidence.get('degraded') else '完整'}",
        "",
        str(output.get("conclusion", "")),
    ]
    original = {
        normalized(item.get("code")): item
        for item in output.get("recommendations", [])
        if isinstance(item, dict)
    }
    for item in items:
        lines.extend(
            [
                "",
                f"## {item['selection_rank']}. {item['stock_name']}（{item['stock_code']}）",
                f"- 总分：{item['final_score']:.1f}",
                f"- 截止点参考价：{item['reference_price']:.2f}",
                f"- 一手含费成本：{item['estimated_lot_cost']:.2f}",
                f"- 摘要：{item['summary']}",
                f"- 量化依据：{item['quant_data']}",
                f"- 新催化：{item['fresh_catalyst']}",
                f"- 旧背景：{item['old_background']}",
                f"- 风险：{item['main_risk']}",
                f"- 取消条件：{item['cancel_conditions']}",
                "",
                "#### 分项评分依据",
                "",
                "| 分项 | 分数 | 依据与0分原因 | 相关可用来源 |",
                "| --- | ---: | --- | --- |",
            ]
        )
        reasons = original.get(item["stock_code"], {}).get("scoreReasons") or {}
        proof = evidence["scoreEvidence"][item["stock_code"]]
        for dimension, label, field in [
            ("market", "市场", "market_score"),
            ("sector", "板块", "sector_score"),
            ("stock", "个股", "stock_score"),
            ("catalyst", "催化", "catalyst_score"),
            ("risk", "风险扣分", "risk_deduction"),
        ]:
            reason = reasons.get(dimension) or "模型未提供独立解释；已有依据：" + str(
                item.get("quant_data", "")
            )
            if dimension in ("sector", "catalyst"):
                reason = proof[dimension + "State"] + "；" + reason
            lines.append(f"| {label} | {item[field]:.1f} | {cell(reason)} | {cell(item['source_refs'])} |")
    if warnings:
        lines.extend(["", "## 校验说明", *("- " + warning for warning in warnings)])
    if run["archive_reason"]:
        lines.extend(["", "> " + run["archive_reason"]])
    return "\n".join(lines)
