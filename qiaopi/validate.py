"""资料结构与核账规则的自检。

分两类：

* 结构错误（``error``）：引用不存在、重复编号、账页指向不存在的户等，必须修。
* 核账警示（``warning``）：有笔迹候选但只采用了一个值而未说明依据、采用
  金额缺币种、账页新版本试图覆盖旧页等，需要人复核。

自检只报告，不替人改数，也不以“账面刚好对平”作为通过条件。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Corpus, Record, ROLES


@dataclass
class Finding:
    level: str
    code: str
    where: str
    message: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "level": self.level, "code": self.code, "where": self.where,
            "message": self.message, "detail": self.detail,
        }


def _line_where(rec: Record, line_id: str) -> str:
    return f"{rec.id}:{line_id}"


def validate(corpus: Corpus) -> list[Finding]:
    findings: list[Finding] = []
    ids = corpus.ids()
    duplication = sorted({i for i in ids if ids.count(i) > 1})
    for record_id in duplication:
        findings.append(Finding("error", "duplicate_record", record_id, "记录编号重复。"))

    positions = {r.id: r.arrival_order for r in corpus.records}
    for rec in corpus.records:
        if rec.arrival_order <= 0:
            findings.append(Finding("error", "bad_arrival_order", rec.id, "arrival_order 必须为正整数。"))
        if rec.revealed_at < 1:
            findings.append(Finding("error", "bad_reveal", rec.id, "revealed_at 必须 ≥ 1。"))
        for ref in rec.references:
            if ref not in positions:
                findings.append(Finding("error", "dangling_reference", rec.id, f"引用不存在的记录 {ref}。"))
            elif positions[ref] >= rec.arrival_order:
                findings.append(Finding(
                    "error", "reference_not_earlier", rec.id,
                    f"引用 {ref} 的收件次序不早于本件（{positions[ref]} ≥ {rec.arrival_order}）。",
                ))
        if rec.kind == "银信" and not rec.party:
            findings.append(Finding("warning", "letter_without_party", rec.id, "银信未注明家户，归户只能依正文和封标。"))
        if rec.kind == "家户" and not rec.party:
            findings.append(Finding("warning", "household_without_party", rec.id, "家户记录未连到 households 目录。"))
        if rec.party and rec.party not in [h["id"] for h in corpus.households]:
            findings.append(Finding("error", "unknown_party", rec.id, f"家户 {rec.party} 不在 households 中。"))

        for year in rec.year_candidates:
            values = [int(v) for v in year.get("value_candidates") or []]
            chosen = year.get("chosen")
            if not values:
                findings.append(Finding(
                    "error", "year_candidate_empty", rec.id,
                    "年份候选没有列出任何值。",
                ))
            if chosen is not None and int(chosen) not in values:
                findings.append(Finding(
                    "error", "year_chosen_not_candidate", rec.id,
                    f"采用年份 {chosen} 不在候选 {values} 中。",
                ))
            if chosen is None and year.get("status") != "unresolved":
                findings.append(Finding(
                    "warning", "year_unresolved_without_status", rec.id,
                    "年份未采用，但状态未标为 unresolved；复核时容易误当成已定年。",
                ))

        for line in rec.lines:
            where = _line_where(rec, line.id)
            if line.role not in ROLES:
                findings.append(Finding("error", "unknown_role", where, f"未知行角色 {line.role}。"))
            for amount in line.amounts:
                if amount.get("currency") is None:
                    findings.append(Finding(
                        "warning", "adopted_without_currency", where,
                        "采用了金额却没有币种；应分币种保留，不得默认折银元。",
                        {"amount": amount},
                    ))
            candidates = line.amount_candidates
            distinct = {c.get("value") for c in candidates if c.get("value") is not None}
            if len(distinct) > 1 and line.amounts:
                explained = bool(line.note) or any(a.get("note") for a in line.amounts)
                if not explained:
                    findings.append(Finding(
                        "warning", "ambiguous_candidate_without_reason", where,
                        "笔迹有多个候选，却直接采用了其中一个值而未说明依据。",
                        {"candidates": candidates, "adopted": line.amounts},
                    ))
            if line.covers and len(line.covers) > 1 and line.amounts and not line.allocation and not line.pointing:
                findings.append(Finding(
                    "warning", "shared_receipt_without_pointing", where,
                    "一条凭据覆盖多批并给了金额，但没有文本指向或明示分配。",
                ))
            if line.genuine_delivery is None and line.role == "signoff":
                findings.append(Finding(
                    "warning", "signoff_unmarked", where,
                    "签收行未标明是否为确已交付；核账不会把它计为实收。",
                ))

    households = {h["id"] for h in corpus.households}
    versions = corpus.ledger
    for version in corpus.ledger_versions:
        if version.supersedes and version.supersedes not in versions:
            findings.append(Finding(
                "error", "unknown_superseded_version", version.version,
                f"新版本声明的旧版本 {version.supersedes} 不存在。",
            ))
        for page_id, page in version.pages.items():
            for entry in page.get("entries") or []:
                if entry.get("household") not in households:
                    findings.append(Finding(
                        "error", "ledger_unknown_household", f"{version.version}/{page_id}",
                        f"账页条目家户 {entry.get('household')} 不在 households 中。",
                        {"entry": entry},
                    ))

    for rec in corpus.records:
        if not rec.citation:
            continue
        version = rec.citation.get("ledger")
        page = rec.citation.get("page")
        if version not in versions:
            findings.append(Finding("error", "citation_unknown_ledger", rec.id, f"引用版本 {version} 不存在。"))
        elif page not in versions[version].pages:
            findings.append(Finding("error", "citation_unknown_page", rec.id, f"引用页 {page} 不存在。"))
        else:
            entries = versions[version].pages[page].get("entries") or []
            sealed_on_page = {e.get("sealed") for e in entries}
            for quoted in rec.citation.get("quoted") or []:
                if quoted.get("sealed") not in sealed_on_page:
                    findings.append(Finding(
                        "error", "citation_entry_missing", rec.id,
                        f"引用条目 {quoted.get('sealed')} 不在 {version}/{page} 页上。",
                    ))

    for rec in corpus.records:
        if not rec.revision:
            continue
        target = rec.revision.get("target")
        if target and target not in positions:
            findings.append(Finding("error", "revision_unknown_target", rec.id, f"复核目标 {target} 不存在。"))
        for item in rec.revision.get("reopen") or []:
            line_id = str(item.get("line", ""))
            rec_id = line_id.split(":", 1)[0] if ":" in line_id else None
            if rec_id is None or rec_id not in positions:
                findings.append(Finding(
                    "error", "reopen_unknown_record", rec.id,
                    f"重开条目 {line_id!r} 无法定位记录。",
                ))
                continue
            target_record = corpus.by_id(rec_id)
            if line_id not in [ln.id for ln in target_record.lines]:
                findings.append(Finding(
                    "error", "reopen_unknown_line", rec.id,
                    f"重开条目 {line_id!r} 在 {rec_id} 中不存在。",
                ))
            if not item.get("note"):
                findings.append(Finding(
                    "warning", "reopen_without_note", rec.id,
                    f"重开 {line_id!r} 未写原因与须回看的共用凭据。",
                ))
        for item in rec.revision.get("changes") or []:
            if not item.get("because"):
                findings.append(Finding(
                    "warning", "revision_without_evidence", rec.id,
                    f"复核改动 {item.get('sealed')}/{item.get('field')} 未列凭据记录。",
                ))
            for ref in item.get("because") or []:
                if ref not in positions:
                    findings.append(Finding(
                        "error", "revision_dangling_evidence", rec.id,
                        f"复核改动所凭记录 {ref} 不存在。",
                    ))

    # 已刊账页被引用后，新版本只能 supersedes，不能改旧页内容。
    for version in corpus.ledger_versions:
        if not version.supersedes:
            continue
        base = versions.get(version.supersedes)
        if base is None:
            continue
        for page_id, page in version.pages.items():
            base_page = base.pages.get(page_id)
            if not base_page:
                continue
            base_entries = {e.get("sealed"): e for e in base_page.get("entries") or []}
            for entry in page.get("entries") or []:
                old = base_entries.get(entry.get("sealed"))
                if old is not None and old != entry and not page.get("changes"):
                    findings.append(Finding(
                        "warning", "silent_page_change", f"{version.version}/{page_id}",
                        "新版本改动了旧页条目，却没有列出 changes；引用差异将无法说明。",
                    ))

    return findings


def has_errors(findings: list[Finding]) -> bool:
    return any(item.level == "error" for item in findings)
