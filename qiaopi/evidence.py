"""证据分层：谁看见什么、把款归给哪一户、哪一笔币种有据。

本模块不写账，只提供核账所需的判断及其依据。所有判断都带
``record:line`` 和原文引句，后续报告与复核都以这里返回的对象为准。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from .model import Basis, Corpus, Line, Record

# 归户结果。
RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
MISSING = "missing"


@dataclass
class PartyResolution:
    """一条记录/行的归户判断。"""

    household: str | None
    status: str
    basis: list[Basis] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "household": self.household,
            "status": self.status,
            "basis": [b.as_dict() for b in self.basis],
            "candidates": list(self.candidates),
            "note": self.note,
        }


def _basis(record: Record, line: Line | None, evidence: str, rule: str = "", quote: str | None = None) -> Basis:
    return Basis(
        record=record.id,
        line=line.id if line else "",
        quote=quote if quote is not None else (line.text if line else record.text),
        evidence=evidence,
        rule=rule,
    )


def _alias_tokens(household: dict) -> set[str]:
    tokens = {household.get("name", ""), household.get("village", "")}
    tokens.update(household.get("aliases") or [])
    tokens.update(household.get("addressees") or [])
    return {t for t in tokens if t}


def resolve_party(corpus: Corpus, record: Record, line: Line | None = None) -> PartyResolution:
    """按“住地＋姓名 > 明示户号 > 姓名 > 别名”的次序归户。

    单字别名（如“兰”）登记为歧义别名时，必须另有住地或本人署名才能归户，
    不因同一位代领人经手多户而合并。
    """

    household_ids = [h["id"] for h in corpus.households]
    text = " ".join(filter(None, [record.text, line.text if line else ""]))

    # 转写时已经逐条给出归户依据的，优先采用该依据（它应包含住地、姓名等原文）。
    if record.party in household_ids and record.party_basis:
        target = corpus.household(record.party)
        explicit = line.addressee if line is not None else None
        if explicit in (None, target["id"]):
            context_text = " ".join(b.get("quote", "") for b in record.party_basis)
            if line is None or not context_text or any(
                token in context_text + text for token in _alias_tokens(target)
            ):
                return PartyResolution(
                    household=target["id"],
                    status=RESOLVED,
                    basis=[Basis.from_dict(b) for b in record.party_basis],
                )

    if line is not None and line.addressee in household_ids:
        target = corpus.household(line.addressee)
        return PartyResolution(
            household=target["id"],
            status=RESOLVED,
            basis=[_basis(record, line, f"行内明示收件人为 {target['name']}（{target['id']}）。", "明示户号")],
        )

    village_hits = [h for h in corpus.households if h.get("village") and h["village"] in text]
    if len(village_hits) == 1:
        target = village_hits[0]
        return PartyResolution(
            household=target["id"],
            status=RESOLVED,
            basis=[_basis(record, line, f"文字出现住地“{target['village']}”，指向 {target['name']}（{target['id']}）。", "住地归户")],
        )

    if record.party in household_ids:
        # 转写户号若无附依据，只作提示；文字里仍须有唯一、非歧义的指向。
        target = corpus.household(record.party)
        tokens = _alias_tokens(target)
        ambiguous = set(target.get("ambiguous_aliases") or [])
        hits = [t for t in tokens if t and t not in ambiguous and t in text]
        if hits:
            return PartyResolution(
                household=target["id"],
                status=RESOLVED,
                basis=[_basis(
                    record, line,
                    f"转写记为 {target['name']}（{target['id']}），文字中出现“{hits[0]}”与之相符。",
                    "转写户号＋文字复核",
                )],
            )

    # 转写户号若无文字佐证，仍作归户，但依据要标明只是转写判断，
    # 以便复核时优先回看原文，而不是把它当成已证实的归属。
    if record.party in household_ids:
        target = corpus.household(record.party)
        if record.party_basis:
            return PartyResolution(
                household=target["id"],
                status=RESOLVED,
                basis=[Basis.from_dict(b) for b in record.party_basis],
            )
        return PartyResolution(
            household=target["id"],
            status=RESOLVED,
            basis=[_basis(
                record, line,
                f"转写时判归 {target['name']}（{target['id']}），原文未见住地或姓名佐证。",
                "转写归户（未附文字佐证，须复核）",
            )],
        )

    # 姓名/住地整词命中优先于单字别名。
    name_hits = []
    for h in corpus.households:
        if h.get("name") and h["name"] in text:
            name_hits.append(h)
    if len(name_hits) == 1:
        target = name_hits[0]
        return PartyResolution(
            household=target["id"],
            status=RESOLVED,
            basis=[_basis(record, line, f"文字出现户名“{target['name']}”。", "户名归户")],
        )

    ambiguous = set()
    for h in corpus.households:
        ambiguous.update(h.get("ambiguous_aliases") or [])
    matched: dict[str, list[str]] = {}
    for h in corpus.households:
        for token in _alias_tokens(h):
            if token in text:
                matched.setdefault(token, []).append(h["id"])

    shared_tokens = [t for t in matched if t in ambiguous]
    if shared_tokens:
        candidates: list[str] = []
        for token in shared_tokens:
            for h in corpus.households:
                if any(token in alias for alias in _alias_tokens(h)):
                    if h["id"] not in candidates:
                        candidates.append(h["id"])
        return PartyResolution(
            household=None,
            status=AMBIGUOUS,
            candidates=sorted(candidates),
            note=f"单字别名 {shared_tokens[0]!r} 同时可能指 " + "、".join(candidates) + "；须有住地或本人署名证据。",
        )

    unique_hits = [h for h in corpus.households if h["id"] in {cid for ids in matched.values() for cid in ids}]
    if len(unique_hits) == 1:
        target = unique_hits[0]
        return PartyResolution(
            household=target["id"],
            status=RESOLVED,
            basis=[_basis(record, line, f"文字出现别名，仅指向 {target['name']}（{target['id']}）。", "唯一别名归户")],
        )
    if len(unique_hits) > 1:
        return PartyResolution(
            household=None,
            status=AMBIGUOUS,
            candidates=sorted(h["id"] for h in unique_hits),
            note="文字中的别名同时指向多户，不能合并。",
        )

    return PartyResolution(household=None, status=MISSING, note="文字中没有可归户的姓名、住地或户名。")


@dataclass
class EvidenceContext:
    """截至某一披露次序可见的全部记录，以及复核带来的改写。"""

    corpus: Corpus
    gate: int
    overrides: dict[tuple[str, str], dict] = field(default_factory=dict)
    revisions: list[Record] = field(default_factory=list)

    @classmethod
    def build(cls, corpus: Corpus, gate: int | None = None) -> "EvidenceContext":
        max_gate = max((r.revealed_at for r in corpus.records), default=1)
        gate = max_gate if gate is None else gate
        ctx = cls(corpus=corpus, gate=gate)
        for rec in ctx.records:
            if rec.kind != "复核" or not rec.revision:
                continue
            ctx.revisions.append(rec)
            for item in rec.revision.get("reopen") or []:
                key = (str(item.get("record") or rec.revision.get("target_record") or ""), str(item.get("line", "")))
                # reopen 条目可只给 line，记录号从 line id 前缀取。
                if not key[0] and ":" in key[1]:
                    key = (key[1].split(":", 1)[0], key[1])
                if item.get("field") and item.get("to") is not None:
                    ctx.overrides.setdefault(key, {})[item["field"]] = item["to"]
        return ctx

    @property
    def records(self) -> list[Record]:
        return sorted(
            (r for r in self.corpus.records if r.revealed_at <= self.gate),
            key=lambda r: (r.arrival_order, r.id),
        )

    def effective_line(self, record: Record, line: Line) -> Line:
        """套用复核改写后的行视图；原文本身不改。"""

        changes = self.overrides.get((record.id, line.id))
        if not changes:
            return line
        payload = line.as_dict()
        payload.update(changes)
        payload["reopened_by_review"] = True
        return Line.from_dict(payload)

    def record(self, record_id: str) -> Record:
        for rec in self.records:
            if rec.id == record_id:
                return rec
        raise KeyError(f"截至第 {self.gate} 步未见记录 {record_id!r}")

    def all_records_including_hidden(self) -> list[Record]:
        return sorted(self.corpus.records, key=lambda r: (r.arrival_order, r.id))

    def is_primary(self, record: Record) -> bool:
        """引用与复核不是新的到款凭证，只用于对账与改动说明。"""

        return record.kind not in ("引用", "复核")

    def provenance(self, record: Record) -> list[str]:
        notes = []
        if record.authenticity == "copy":
            notes.append(f"此为抄件，原件为 {record.copy_of}。")
        if record.protocol and record.protocol != "original":
            notes.append(f"录文方式：{record.protocol}。")
        return notes
