"""账页对账：旧账页原样保留，当前分账另算，逐条列出引用所凭的分账变化。

旧账页一经引用就作为版本快照保存，本模块不修改它。对账输出同时给出
页上的号（版本＋页码＋户＋封标）和当前账本里的对应条目，便于研究员
从旧引用跳回原页，再看到这次复核把哪一条改了、凭哪几件记录改的。
"""
from __future__ import annotations

import copy
import json

from .allocator import triage
from .model import Corpus, LedgerVersion, format_amount

STATUS_LABELS = {
    "signed": "家户收讫",
    "signed_partial": "部分到款",
    "proxy_unverified": "他人代领，无家户签收",
    "forwarded": "已转交，未见签收",
    "no_signoff": "未见签收",
    "returned": "批局退回",
    "no_evidence": "无到款证据",
    "unresolved": "未分",
}


def citations_of(corpus: Corpus, version: str | None = None, page: str | None = None) -> list[dict]:
    """找出所有引用了某版本/某页的记录。"""

    found = []
    for rec in corpus.records:
        cite = rec.citation
        if not cite:
            continue
        if version is not None and cite.get("ledger") != version:
            continue
        if page is not None and cite.get("page") != page:
            continue
        found.append({
            "record": rec.id,
            "kind": rec.kind,
            "ledger": cite.get("ledger"),
            "page": cite.get("page"),
            "quoted": copy.deepcopy(cite.get("quoted") or []),
            "note": cite.get("note", ""),
        })
    return found


def _current_entry(book: dict, sealed: str) -> dict | None:
    batch = book["batches"].get(sealed)
    if batch is None:
        return None
    return {
        "household": batch.get("household"),
        "amount": {
            "value": batch.get("signed_totals", {}).get("银元", 0),
            "currency": "银元",
        },
        "amounts_by_currency": dict(batch.get("signed_totals") or {}),
        "status": "signed" if batch.get("signed") else "no_signoff",
        "evidence": [hit["record"] + ":" + hit["line"] for hit in batch.get("signed") or []],
    }


def _why_changed(book: dict, household: str | None, sealed: str, field: str) -> list[dict]:
    """说明新值凭哪几件记录支持。"""

    reasons = []
    if household:
        entry = book["households"].get(household)
        if entry:
            for hit in entry["signed"]:
                if hit.get("batch") == sealed:
                    reasons.append({
                        "record": hit["record"],
                        "line": hit["line"],
                        "quote": hit["quote"],
                        "basis": hit["basis"],
                        "amount": hit["amount"],
                    })
    if not reasons:
        for item in book["unallocated"]:
            if item.get("record") and sealed and sealed in (item.get("quote") or ""):
                reasons.append({
                    "record": item["record"], "line": item["line"],
                    "quote": item["quote"], "reason": item.get("note"),
                })
    return reasons


def reconcile(corpus: Corpus, book: dict) -> dict:
    """把每个已刊版本逐页对照当前账本。"""

    versions = []
    revision_map: dict[tuple[str, str], list[dict]] = {}
    for rec in corpus.records:
        revision = rec.revision or {}
        for item in revision.get("changes") or []:
            if rec.revealed_at > book.get("gate", 10**9):
                continue
            key = (str(item.get("sealed")), str(item.get("field")))
            revision_map.setdefault(key, []).append({
                "revision": rec.id,
                "record": rec.id,
                "from": item.get("from"),
                "to": item.get("to"),
                "because": list(item.get("because") or []),
            })
    for version in corpus.ledger_versions:
        pages = []
        for page_id, page in version.pages.items():
            rows = []
            for entry in page.get("entries") or []:
                sealed = entry.get("sealed")
                current = _current_entry(book, sealed) if sealed else None
                changed = []
                if current is None:
                    changed.append({"field": "entry", "from": "present", "to": "missing"})
                else:
                    cited_amount = entry.get("amount") or {}
                    if cited_amount.get("value") != current["amount"]["value"]:
                        changed.append({
                            "field": "amount",
                            "from": dict(cited_amount),
                            "to": dict(current["amount"]),
                            "because": _why_changed(book, current.get("household"), sealed, "amount"),
                        })
                    if entry.get("status") != current["status"]:
                        changed.append({
                            "field": "status",
                            "from": entry.get("status"),
                            "to": current["status"],
                            "because": _why_changed(book, current.get("household"), sealed, "status"),
                        })
                for change in changed:
                    declared = revision_map.get((str(sealed), str(change["field"])), [])
                    if declared:
                        change["declared_by"] = [
                            {"revision": item["revision"], "because": item["because"]}
                            for item in declared
                        ]
                rows.append({
                    "household": entry.get("household"),
                    "sealed": sealed,
                    "cited": copy.deepcopy(entry),
                    "current": copy.deepcopy(current),
                    "changed": changed,
                })
            pages.append({
                "page": page_id,
                "title": page.get("title", ""),
                "locator": f"{version.version}/{page_id}",
                "cited_by": citations_of(corpus, version.version, page_id),
                "entries": rows,
                "summary_cited": copy.deepcopy(page.get("summary") or {}),
            })
        versions.append({
            "version": version.version,
            "period": version.period,
            "supersedes": version.supersedes,
            "revealed_at": version.revealed_at,
            "note": version.note,
            "pages": pages,
            "unchanged": all(not row["changed"] for page in pages for row in page["entries"]),
        })
    return {"versions": versions, "current_gate": book.get("gate")}


def cited_claims_changed(report: dict) -> list[dict]:
    """只列出引用所凭的分账确已变化的条目，供研究员逐条核对。"""

    rows = []
    for version in report["versions"]:
        for page in version["pages"]:
            for entry in page["entries"]:
                if not entry["changed"]:
                    continue
                rows.append({
                    "locator": page["locator"],
                    "household": entry["household"],
                    "sealed": entry["sealed"],
                    "cited_by": [c["record"] for c in page["cited_by"]],
                    "changes": entry["changed"],
                })
    return rows


def current_page(book: dict, base: str | None = None) -> dict:
    """按当前证据重排一页；不写入任何已刊版本，只作对照。"""

    entries = []
    for household_id, entry in book["households"].items():
        for item in entry.get("face") or []:
            received = entry.get("received_totals") or {}
            entries.append({
                "household": household_id,
                "sealed": item.get("batch"),
                "face": {"value": item.get("value"), "currency": item.get("currency")},
                "amount": {
                    "value": received.get(item.get("currency"), 0),
                    "currency": item.get("currency"),
                },
                "status": entry.get("status"),
                "evidence": [f"{hit['record']}:{hit['line']}" for hit in entry.get("signed") or []
                             if hit.get("batch") == item.get("batch")],
                "note": "按当前证据重排；封面与签收差额见 households.open_balance。",
            })
    totals: dict[str, int] = {}
    for entry in book["households"].values():
        for cur, value in (entry.get("received_totals") or {}).items():
            totals[cur] = totals.get(cur, 0) + value
    outstanding = [
        {"household": hid, **bal}
        for hid, entry in book["households"].items() for bal in entry.get("open_balance") or []
        if bal.get("value")
    ]
    return {
        "supersedes": base,
        "gate": book.get("gate"),
        "entries": entries,
        "summary": {
            "received": {cur: {"value": v, "currency": cur} for cur, v in totals.items()},
            "outstanding": outstanding,
        },
        "triage": [
            {"record": row["record"], "line": row["line"],
             "missing": row["missing"], "note": row["note"]}
            for row in triage(book)
        ],
        "note": "当前账页只作对照；已刊旧页保持原样，差异见 reconcile。",
    }


def revision_version(book: dict, base: str, name: str, period: str = "") -> LedgerVersion:
    """由当前账本生成新版页对象（不自动写入 corpus）。"""

    page = current_page(book, base)
    return LedgerVersion(
        version=name,
        period=period,
        pages={"T08": {
            "title": period or "按当前证据重排",
            "entries": page["entries"],
            "summary": page["summary"],
        }},
        supersedes=base,
        revealed_at=book.get("gate", 1),
        note="由核账结果重排；旧版本保持原样，引用差异见 reconcile。",
    )


def _change_value(value) -> str:
    if isinstance(value, dict) and "value" in value:
        return format_amount(value.get("value"), value.get("currency"))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return STATUS_LABELS.get(value, str(value))


def render_page(report: dict, locator: str) -> str:
    """按页印刷旧账页的原文，字句不改。"""

    for version in report["versions"]:
        for page in version["pages"]:
            if page["locator"] != locator:
                continue
            lines = [f"[{page['locator']}] {page['title']}"]
            for entry in page["entries"]:
                cited = entry["cited"]
                amount = cited.get("amount") or {}
                lines.append(
                    f"  {entry['sealed']}／{entry['household']}："
                    f"{format_amount(amount.get('value'), amount.get('currency'))}，"
                    f"状态 {STATUS_LABELS.get(cited.get('status'), cited.get('status'))}；"
                    f"{cited.get('note', '')}"
                )
            lines.append("  引用：" + ("、".join(c["record"] for c in page["cited_by"]) or "无"))
            if any(entry["changed"] for entry in page["entries"]):
                lines.append("  当前复核已改动：")
                for entry in page["entries"]:
                    for change in entry["changed"]:
                        details = "；".join(
                            f"{b.get('record')}:{b.get('line')}" for b in change.get("because") or []
                        )
                        lines.append(
                            f"    {entry['sealed']} {change['field']}: "
                            f"{_change_value(change['from'])} → {_change_value(change['to'])}"
                            + (f"（凭 {details}）" if details else "")
                        )
                        if change.get("declared_by"):
                            who = "、".join(f"{item['revision']}（{'、'.join(item['because'])}）"
                                           for item in change["declared_by"])
                            lines.append(f"      复核来源：{who}")
            return "\n".join(lines)
    raise KeyError(f"没有账页 {locator!r}")


def to_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
