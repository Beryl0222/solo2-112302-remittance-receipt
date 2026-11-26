"""把核账结果渲染成研究同事能用的报告和查询表。"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from .allocator import readings_pending, settle, signoff_sequence, triage
from .evidence import resolve_party
from .ledger import cited_claims_changed, current_page, reconcile, render_page
from .model import Corpus, format_amount


STATUS_TEXT = {
    "signed": "家户已签收（与封面相符）",
    "signed_partial": "部分到款，余数未见签收",
    "proxy_unverified": "他人代领，无家户签收",
    "forwarded": "已转交/在批局，未见签收",
    "no_signoff": "未见任何签收",
    "returned": "批局退回",
    "no_evidence": "无到款证据",
}


def _fmt_amount(value: int | None, currency: str | None) -> str:
    return format_amount(value, currency)


def summarize(book: dict) -> str:
    lines = [f"证据截至第 {book['gate']} 步。金额以辅币记，银元 1 元 = 100 分。", ""]
    for hid, entry in book["households"].items():
        totals = entry["received_totals"]
        rendered = "、".join(_fmt_amount(v, c) for c, v in totals.items()) or "无签收"
        lines.append(f"{hid} {entry['name']}：{STATUS_TEXT.get(entry['status'], entry['status'])}；家户实收 {rendered}。")
        for bal in entry["open_balance"]:
            lines.append(f"  未对平：{_fmt_amount(bal['value'], bal['currency'])}（{bal['note']}）")
        for note in entry["status_notes"]:
            lines.append(f"  状态：{note}")
    if book.get("custody_events"):
        lines.append("")
        lines.append("代领/保管（不计家户实收）：")
        for event in book["custody_events"]:
            who = "、".join(event.get("households") or []) or "户属未定"
            lines.append(
                f"  {event['record']}:{event['line']} {event.get('proxy') or '他人'} 领 {who}；"
                f"“{event['text']}”"
            )
    pending = triage(book)
    if pending:
        lines.append("")
        lines.append("未分/未决：")
        for row in pending:
            lines.append(
                f"  {row['record']}:{row['line']} — {'、'.join(row['missing'])}；"
                f"“{row['quote']}”；{row['note']}"
            )
    readings = readings_pending(book)
    if readings:
        lines.append("")
        lines.append("读法暂未采用（只在报告中出现，不入账）：")
        for row in readings:
            labels = {"ambiguous_reading": "读法未定", "missing_currency": "缺币种依据",
                      "reopened_by_review": "复核已重开、待重新判定"}
            why = "、".join(labels.get(r, r) for r in row["reasons"]) or "未注原因"
            lines.append(
                f"  {row['record']}:{row['line']} — {why}；"
                f"候选 {'/'.join(str(c.get('reading', c.get('value'))) for c in row['candidates'])}"
            )
    return "\n".join(lines)


def household_csv(book: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["家户", "户名", "状态", "币种", "实收", "封面额", "差额"])
    for hid, entry in book["households"].items():
        for cur in sorted(set(entry["received_totals"]) | set(entry["face_totals"])):
            received = entry["received_totals"].get(cur, 0)
            face = entry["face_totals"].get(cur, 0)
            writer.writerow([hid, entry["name"], STATUS_TEXT.get(entry["status"], entry["status"]),
                             cur, received, face, face - received])
    return buffer.getvalue()


def unresolved_csv(book: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["来源", "行", "缺项", "币种", "金额候选", "指向", "原文", "说明"])
    for row in triage(book):
        writer.writerow([
            row["record"], row["line"], "、".join(row["missing"]),
            "、".join(sorted({str(a.get("currency")) for a in row["amounts"]}) or
                     sorted({str(c.get("currency")) for c in row["candidates"]})),
            "；".join(str(c.get("reading", c.get("value"))) for c in row["candidates"]),
            "、".join(row["household_candidates"]),
            row["quote"], row["note"],
        ])
    return buffer.getvalue()


def document(book: dict, corpus: Corpus) -> dict:
    report = reconcile(corpus, book)
    return {
        "gate": book["gate"],
        "households": book["households"],
        "batches": book["batches"],
        "unallocated": book["unallocated"],
        "custody_events": book.get("custody_events", []),
        "reopen_impact": book.get("reopen_impact", []),
        "signoff_sequence": signoff_sequence(book),
        "current_page": current_page(book),
        "readings_pending": readings_pending(book),
        "triage": triage(book),
        "ledger": report,
        "cited_claims_changed": cited_claims_changed(report),
    }


def write_all(outdir: str | Path, book: dict, corpus: Corpus) -> dict[str, Path]:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    payload = document(book, corpus)
    paths: dict[str, Path] = {}

    def write(name: str, text: str) -> None:
        path = outdir / name
        path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        paths[name] = path

    write("summary.txt", summarize(book))
    write("households.csv", household_csv(book))
    write("unresolved.csv", unresolved_csv(book))
    write("book.json", json.dumps(payload, ensure_ascii=False, indent=2))
    for version in payload["ledger"]["versions"]:
        for page in version["pages"]:
            name = page["locator"].replace("/", "_") + ".txt"
            write(name, render_page(payload["ledger"], page["locator"]))
    return paths


def statement_text(book: dict, corpus: Corpus, household_id: str) -> str:
    from .allocator import household_statement

    stmt = household_statement(book, household_id)
    lines = [f"{stmt['household']} {stmt['name']} — {STATUS_TEXT.get(stmt['status'], stmt['status'])}"]
    for cur, value in stmt["received_totals"].items():
        lines.append(f"  家户实收 {_fmt_amount(value, cur)}")
    for item in stmt["open_balance"]:
        lines.append(f"  差额 {_fmt_amount(item['value'], item['currency'])}：{item['note']}")
    if stmt["lines"]:
        lines.append("  逐笔：")
    for item in stmt["lines"]:
        amount = item["amount"]
        lines.append(
            f"    [{item['kind']}] {_fmt_amount(amount.get('value'), amount.get('currency'))} "
            f"← {item['record']}:{item['line']}“{item['quote']}”"
        )
        if item.get("sequence"):
            lines.append(f"      签收次序 {item['sequence']}。")
        lines.append(f"      币种依据：{item.get('currency_basis') or '—'}")
        for basis in item.get("basis") or []:
            lines.append(f"      归户依据：{basis['record']}:{basis['line']} {basis['evidence']}")
        for note in item.get("notes") or []:
            if note:
                lines.append(f"      注：{note}")
    if stmt.get("custody"):
        lines.append("  他人代领/保管（未计入家户实收）：")
        for event in stmt["custody"]:
            lines.append(
                f"    {event['record']}:{event['line']} {event.get('proxy') or '他人'} — "
                f"“{event['text']}”；{event['note']}"
            )
    if stmt["unresolved"]:
        lines.append("  未计入的款项：")
        for item in stmt["unresolved"]:
            lines.append(f"    {item['record']}:{item['line']} — {'、'.join(item['reasons'])}；{item['note']}")
    return "\n".join(lines)


def explain_line(corpus: Corpus, record_id: str, line_id: str) -> str:
    rec = corpus.by_id(record_id)
    line = rec.line(line_id)
    party = resolve_party(corpus, rec, line)
    lines = [
        f"{record_id}:{line_id}（{line.role}）",
        f"原文：{line.text}",
        f"归户：{party.household or '未定'}"
        + (f"（候选 {'、'.join(party.candidates)}）" if party.candidates else ""),
    ]
    for basis in party.basis:
        lines.append(f"  {basis.record}:{basis.line} “{basis.quote}” — {basis.evidence}")
    if line.amount_candidates:
        lines.append("笔迹候选：")
        for cand in line.amount_candidates:
            lines.append(
                f"  {cand.get('reading', cand.get('value'))} = {cand.get('value')}"
                f"（{cand.get('currency') or '币种未定'}；{cand.get('status', '')}）"
            )
    if line.amounts:
        lines.append("采用：")
        for amount in line.amounts:
            lines.append(
                f"  {_fmt_amount(amount.get('value'), amount.get('currency'))}"
                f" — {amount.get('note') or '未注依据'}"
            )
    else:
        lines.append("采用：无（保留候选，不入账）")
    if line.pointing:
        lines.append(f"文本指向：{line.pointing}")
    if line.allocation:
        parts = "、".join(
            f"{code}={_fmt_amount(entry.get('value'), entry.get('currency'))}"
            for code, entry in line.allocation.items()
        )
        lines.append(f"明示分配：{parts}")
    if line.covers and len(line.covers) > 1:
        lines.append(f"覆盖批号：{'、'.join(line.covers)}")
    if line.same_as:
        same = line.same_as
        lines.append(f"“{same.get('raw')}”回指：{same.get('column')}（{same.get('note')}）")
    if line.note:
        lines.append(f"注：{line.note}")
    return "\n".join(lines)


__all__ = [
    "document", "explain_line", "household_csv", "statement_text", "summarize",
    "unresolved_csv", "write_all",
]
