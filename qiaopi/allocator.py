"""分账：把签收、转交与代领按证据落到家户和批号。

原则（与资料中的业务要求一致）：

* 只把有签收依据的金额计作家户实收。封面额、批局合封额、信中嘱托额都不
  自动变成家户实收。
* 币种不明就分币种保留，不折算、不并入银元。
* 一张共用收据/合封可以覆盖多个批号，按签收次序与文字指向分配；两个分配
  都说得通时不替人选择，款项停在未分状态。
* 已经由另一户签收的金额，不会因为某一封“看起来已结”而被移走。
* 代领人相同不等于家户相同；代领只记保管状态，不产生家户实收。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .evidence import AMBIGUOUS, RESOLVED, EvidenceContext, resolve_party
from .model import Amount, Basis, Line, Record

SIGNOFF = "signoff"
TRANSFER_ROLES = frozenset({"forward", "stub"})
PROXY_ROLES = frozenset({"proxy_pickup"})
RETURN_ROLES = frozenset({"return"})

# 未分款项/未决账目的原因。
R_HOUSEHOLD = "missing_household"
R_CURRENCY = "missing_currency"
R_READING = "ambiguous_reading"
R_AWAITING = "awaiting_receipt"
R_CONFLICT = "conflict"
R_OVER = "over_settled"
R_REOPEN = "reopened_by_review"


@dataclass
class Hit:
    """一笔已落到家户或批号的金额。"""

    household: str | None
    batch: str | None
    record: str
    line: str
    role: str
    amount: Amount
    quote: str
    basis: list[dict] = field(default_factory=list)
    sequence: int = 0
    genuine: bool | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "household": self.household,
            "batch": self.batch,
            "record": self.record,
            "line": self.line,
            "role": self.role,
            "amount": self.amount.as_dict(),
            "currency_basis": self.amount.note,
            "quote": self.quote,
            "basis": list(self.basis),
            "sequence": self.sequence,
            "genuine": self.genuine,
            "notes": list(self.notes),
        }


@dataclass
class Unresolved:
    """未能落到家户的分账，附缺什么证据。"""

    record: str
    line: str
    amounts: list[Amount] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    household_candidates: list[str] = field(default_factory=list)
    quote: str = ""
    note: str = ""
    alternatives: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "record": self.record,
            "line": self.line,
            "amounts": [a.as_dict() for a in self.amounts],
            "candidates": list(self.candidates),
            "reasons": list(self.reasons),
            "household_candidates": list(self.household_candidates),
            "quote": self.quote,
            "note": self.note,
            "alternatives": list(self.alternatives),
        }


def _assignments(amounts: list[Amount], codes: list[str]) -> list[dict[str, Amount]]:
    """把若干金额分给若干批号的所有一一对应方式。"""

    if len(amounts) != len(codes):
        return []
    if not amounts:
        return []
    results: list[dict[str, Amount]] = []

    def walk(remaining_amounts: list[Amount], remaining_codes: list[str], acc: dict[str, Amount]) -> None:
        if not remaining_amounts:
            results.append(dict(acc))
            return
        amount = remaining_amounts[0]
        for index, code in enumerate(remaining_codes):
            walk(
                remaining_amounts[1:],
                remaining_codes[:index] + remaining_codes[index + 1:],
                {**acc, code: amount},
            )

    walk(amounts, codes, {})
    return results


def allocate_shared(
    ctx: EvidenceContext,
    batches: dict[str, dict],
    signed: list[Hit],
    rec: Record,
    line: Line,
) -> tuple[list[Hit], Unresolved | None]:
    """一张凭据覆盖多批时，按文本指向和签收次序分配金额。

    只在能唯一确定时采用；两种分配都说得通就返回未决项（附两种分法），
    不为了“看起来已结”去占用另一批已经有据的款项。
    """

    codes = [c for c in (line.covers or []) if c in batches]
    amounts = line.chosen()
    if len(codes) < 2 or not amounts:
        return [], None

    # 有一封还没能归户时，即使金额分配得出来，也不能把款记到“无名”的家户上。
    unresolved_codes = [c for c in codes if not batches[c].get("household")]
    if unresolved_codes:
        reasons = [R_HOUSEHOLD]
        if any(a.currency is None for a in amounts):
            reasons.append(R_CURRENCY)
        return [], Unresolved(
            record=rec.id, line=line.id, amounts=amounts,
            candidates=line.candidate_values(),
            reasons=sorted(set(reasons)),
            household_candidates=[batches[c]["household"] for c in codes if batches[c].get("household")],
            quote=line.text,
            note=(
                f"共用凭据还覆盖未能归户的 {'、'.join(unresolved_codes)}；"
                "须先补齐该封的家户证据，才能按封分配。"
            ),
        )

    # 凭据本身已逐项写明“某封得多少”时，按文字明示的分配。
    # 只有与封面/已签收额冲突的那一项暂缓；其余各项如有独立指向，仍可入账，
    # 不因某一封看似已结而占用另一封的款项。
    if line.allocation:
        mapping: dict[str, Amount] = {}
        problems: list[str] = []
        for code in codes:
            entry = line.allocation.get(code)
            if not entry:
                problems.append(f"共用凭据的明示分配缺少 {code}。")
                continue
            amount = Amount(int(entry["value"]), entry.get("currency"), entry.get("note", ""))
            if amount.currency is None:
                problems.append(f"{code} 的明示分配没有币种。")
                continue
            face = next((a for a in batches[code]["face"] if a.get("currency") == amount.currency), None)
            already = _settled(code, signed).get(amount.currency, 0)
            if face and already + amount.value > int(face.get("value", 0)):
                problems.append(
                    f"明示给 {code} 的 {amount.value} {amount.currency} 会使该批超过封面 "
                    f"{face['value']}（已签收 {already}）；该批款项暂缓，不挪用他批已据之款。"
                )
                continue
            mapping[code] = amount
        hits = _mapping_hits(rec, line, batches, mapping) if mapping else []
        if not problems:
            return hits, None
        return hits, Unresolved(
            record=rec.id, line=line.id,
            amounts=[Amount(int(line.allocation[c]["value"]), line.allocation[c].get("currency"))
                     for c in codes if c in line.allocation and c not in mapping],
            candidates=line.candidate_values(), reasons=[R_CONFLICT],
            household_candidates=sorted({batches[c]["household"] for c in codes if batches[c]["household"]}),
            quote=line.text,
            note="；".join(problems) + ("其余明示项已按各自封标分别入账。" if hits else ""),
        )

    feasible = []
    for mapping in _assignments(amounts, codes):
        ok = True
        for code, amount in mapping.items():
            currency = amount.currency
            if currency is None:
                ok = False
                break
            face = next((a for a in batches[code]["face"] if a.get("currency") == currency), None)
            already = _settled(code, signed).get(currency, 0)
            if face and already + amount.value > int(face.get("value", 0)):
                ok = False
                break
        if ok:
            feasible.append(mapping)

    # 文本指向：明确提到某一批号时，只保留该批号拿到金额的分法。
    pointing = line.pointing or line.text or ""
    mentioned = [code for code in codes if code in pointing]
    if len(mentioned) == 1:
        feasible = [m for m in feasible if m.get(mentioned[0]) is not None]

    # 明确写“按签收次序”时，按签收先后与金额书写先后依次对应；未签收的
    # 批号排在后面。
    if "签收次序" in pointing and len(feasible) > 1:
        order = []
        for code in codes:
            seqs = [h.sequence for h in signed if h.batch == code and h.sequence]
            order.append((min(seqs) if seqs else 10**6, code))
        ordered = [code for _, code in sorted(order)]
        feasible = [
            m for m in feasible
            if all(m.get(code) is amounts[i] for i, code in enumerate(ordered))
        ]

    if len(feasible) == 1:
        return _mapping_hits(rec, line, batches, feasible[0]), None

    alternatives = [
        {
            "mapping": {code: amount.as_dict() for code, amount in mapping.items()},
            "households": {code: batches[code]["household"] for code in mapping},
        }
        for mapping in feasible
    ]
    return [], Unresolved(
        record=rec.id, line=line.id, amounts=amounts,
        candidates=line.candidate_values(), reasons=[R_READING],
        household_candidates=sorted({batches[c]["household"] for c in codes if batches[c]["household"]}),
        quote=line.text,
        note=(
            "共用凭据覆盖多批，文本指向不足以唯一分配"
            + ("；" + line.pointing if line.pointing else "")
            + "。两种分配都说得通，暂不计入任何一户实收。"
        ),
        alternatives=alternatives,
    )


def _batch_index(ctx: EvidenceContext) -> dict[str, dict]:
    """封标/批号到信件与家户的索引；以批封自身的封标为准。"""

    index: dict[str, dict] = {}
    for rec in ctx.records:
        if rec.kind != "银信":
            continue
        party = resolve_party(ctx.corpus, rec)
        for line in rec.lines:
            eff = ctx.effective_line(rec, line)
            if eff.role != "face":
                continue
            codes = [eff.sealed_reference, eff.batch]
            for code in codes:
                if not code:
                    continue
                for part in str(code).replace("、", "+").split("+"):
                    part = part.strip()
                    if not part:
                        continue
                    if part in index and index[part]["letter"] != rec.id:
                        raise ValueError(f"封标 {part} 同时出现在 {index[part]['letter']} 与 {rec.id}")
                    index[part] = {
                        "letter": rec.id,
                        "household": party.household if party.status == RESOLVED else None,
                        "party_status": party.status,
                        "party_basis": [b.as_dict() for b in party.basis],
                        "face": [a.as_dict() for a in eff.chosen()],
                    }
    return index


def _signoff_sequence(ctx: EvidenceContext) -> dict[str, int]:
    """每个批号的签收次序，按收件先后；同批多次签收依次累加。"""

    counts: dict[str, int] = {}
    sequences: dict[str, int] = {}
    for rec in ctx.records:
        for line in rec.lines:
            eff = ctx.effective_line(rec, line)
            if eff.role != SIGNOFF or eff.genuine_delivery is not True:
                continue
            code = eff.sealed_reference or eff.batch or f"?{rec.id}"
            counts[code] = counts.get(code, 0) + 1
            sequences[line.id] = counts[code]
    return sequences


def _currency_of(line: Line) -> str | None:
    if line.currency:
        return line.currency
    amounts = line.chosen()
    if len(amounts) == 1:
        return amounts[0].currency
    return None


def _targets(ctx: EvidenceContext, batches: dict[str, dict], rec: Record, line: Line) -> tuple[list[str], list[str], list[dict]]:
    """文字指向能支持的家户与批号。"""

    households: list[str] = []
    codes: list[str] = []
    basis: list[dict] = []
    for code in (line.sealed_reference, line.batch):
        if not code:
            continue
        for part in str(code).replace("、", "+").split("+"):
            part = part.strip()
            if part and part in batches:
                codes.append(part)
                hh = batches[part]["household"]
                if hh and hh not in households:
                    households.append(hh)
                    basis.append({
                        "record": rec.id, "line": line.id,
                        "quote": line.text,
                        "evidence": f"封标 {part} 系到 {batches[part]['letter']}，该信归 {hh}。",
                        "rule": "封标指向",
                    })
    resolved = resolve_party(ctx.corpus, rec, line)
    if resolved.household and resolved.household not in households:
        households.append(resolved.household)
        basis.extend(b.as_dict() for b in resolved.basis)
    elif resolved.household is None and resolved.status == AMBIGUOUS and not households:
        households.extend(resolved.candidates)
    return households, sorted(set(codes)), basis


def _settled(batch: str, signed: list[Hit]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for hit in signed:
        if hit.batch == batch and hit.amount.currency:
            totals[hit.amount.currency] = totals.get(hit.amount.currency, 0) + hit.amount.value
    return totals


def _mapping_hits(
    rec: Record,
    line: Line,
    batches: dict[str, dict],
    mapping: dict[str, Amount],
) -> list[Hit]:
    hits = []
    for code, amount in mapping.items():
        hits.append(Hit(
            household=batches[code]["household"], batch=code, record=rec.id, line=line.id,
            role=line.role, amount=amount, quote=line.text,
            basis=[{
                "record": rec.id, "line": line.id, "quote": line.text,
                "evidence": (
                    f"共用凭据覆盖 {sorted(mapping)}，按文本指向"
                    + (f"“{line.pointing}”" if line.pointing else "与唯一可行分法")
                    + f"把 {amount.value} {amount.currency} 分到 {code}（{batches[code]['household']}）。"
                ),
                "rule": "文本明示分配优先；无明示时须唯一可行分法",
            }],
            notes=[line.note] if line.note else [],
        ))
    return hits


def _missing_reasons(line: Line) -> list[str]:
    """未采用金额时，区分缺币种、缺读法与待复核重开。"""

    reasons: list[str] = []
    values = {c.get("value") for c in line.candidate_values() if c.get("value") is not None}
    if len(values) > 1 or not values:
        reasons.append(R_READING)
    currencies = {c.get("currency") for c in line.candidate_values()}
    if not currencies or currencies == {None}:
        reasons.append(R_CURRENCY)
    if line.reopened_by_review:
        reasons.append(R_REOPEN)
    return sorted(set(reasons))


def readings_pending(book: dict) -> list[dict]:
    """笔迹有多种读法、尚未采用任何值的行；单独列出，避免被当成已核。"""

    return list(book.get("readings_pending") or [])


def settle(ctx: EvidenceContext) -> dict:
    """按当前可见证据重排全部账目，返回可序列化的账本。"""

    corpus = ctx.corpus
    batches = _batch_index(ctx)
    sequences = _signoff_sequence(ctx)

    signed: list[Hit] = []
    transfers: list[Hit] = []
    unallocated: list[Unresolved] = []
    proxy_pickups: list[Hit] = []
    returns: list[Hit] = []
    household_status_notes: dict[str, list[str]] = {}
    readings_pending: list[dict] = []

    # 第零步：先记下所有“有候选、未采用”的行；它们不参与分账，但须出现在
    # 报告里，读法与币种问题不能被“没有金额”掩盖。
    for rec in ctx.records:
        for line in rec.lines:
            eff = ctx.effective_line(rec, line)
            if eff.chosen() or not eff.candidate_values():
                continue
            readings_pending.append({
                "record": rec.id, "line": line.id, "role": eff.role,
                "text": eff.text,
                "candidates": eff.candidate_values(),
                "reasons": _missing_reasons(eff),
                "note": eff.note,
            })

    # 第一步：签收。只有带明确家户、真实签收且有币种金额的才入家户实收。
    for rec in ctx.records:
        for line in rec.lines:
            eff = ctx.effective_line(rec, line)
            if eff.role != SIGNOFF:
                continue
            party = resolve_party(corpus, rec, eff)
            amounts = eff.chosen()
            code = eff.sealed_reference or eff.batch
            if not amounts:
                unallocated.append(Unresolved(
                    record=rec.id, line=line.id, candidates=eff.candidate_values(),
                    reasons=_missing_reasons(eff),
                    quote=eff.text, note=eff.note or "签收金额尚无确定读法。",
                ))
                continue
            # 一张收据覆盖多批时，按批号归属分配，不要求收据本身先归到一户。
            if len(eff.covers) >= 2:
                if eff.genuine_delivery is not True:
                    unallocated.append(Unresolved(
                        record=rec.id, line=line.id, amounts=amounts,
                        reasons=[R_HOUSEHOLD], quote=eff.text,
                        note="无“确已交付”标记的签收，不计家户实收。",
                    ))
                    continue
                hits, problem = allocate_shared(ctx, batches, signed, rec, eff)
                # 明示分配中有据的部分先入账；与封面冲突的那一项另列未决。
                for hit in hits:
                    hit.genuine = True
                    hit.sequence = sequences.get(line.id, 0)
                    signed.append(hit)
                if problem is not None:
                    unallocated.append(problem)
                continue
            if party.status != RESOLVED:
                # 收据上的别名本身分不清户时，若封标唯一系到一封信、且别名
                # 与该户相符，可由“封标＋别名”共同归户；仍缺封标就保留。
                batch_household = None
                if code and code in batches:
                    batch_household = batches[code]["household"]
                alias_matches = batch_household in party.candidates if batch_household else False
                if batch_household and (alias_matches or not party.candidates):
                    party = type(party)(
                        household=batch_household,
                        status=RESOLVED,
                        basis=list(party.basis) + [Basis(
                            record=rec.id, line=line.id, quote=eff.text,
                            evidence=(
                                f"收据别名单独不能分户，但封标 {code} 只系到 {batch_household}，"
                                f"别名候选 {party.candidates or '无'} 与之相符。"
                            ),
                            rule="封标唯一时可由封标与别名共同归户",
                        )],
                        candidates=party.candidates,
                    )
                else:
                    unallocated.append(Unresolved(
                        record=rec.id, line=line.id, amounts=amounts,
                        candidates=eff.candidate_values(),
                        reasons=[R_HOUSEHOLD],
                        household_candidates=party.candidates,
                        quote=eff.text,
                        note=party.note or "签收人无法唯一归户，不能据此计入任何家户实收。",
                    ))
                    continue
            if code and code in batches and batches[code]["household"] not in (None, party.household):
                unallocated.append(Unresolved(
                    record=rec.id, line=line.id, amounts=amounts,
                    reasons=[R_CONFLICT], quote=eff.text,
                    household_candidates=[party.household, batches[code]["household"]],
                    note=f"签收归户 {party.household} 与封标 {code} 对应的 {batches[code]['household']} 相冲突，暂不入账。",
                ))
                continue
            if eff.genuine_delivery is not True:
                unallocated.append(Unresolved(
                    record=rec.id, line=line.id, amounts=amounts,
                    reasons=[R_HOUSEHOLD], quote=eff.text,
                    note="无“确已交付”标记的签收，不计家户实收。",
                ))
                continue
            basis = [b.as_dict() for b in party.basis]
            for amount in amounts:
                signed.append(Hit(
                    household=party.household, batch=code, record=rec.id, line=line.id,
                    role=eff.role, amount=amount, quote=eff.text, basis=basis,
                    sequence=sequences.get(line.id, 0),
                    genuine=True, notes=[eff.note] if eff.note else [],
                ))

    # 第二步：转交/底联。可凭封标、住地或姓名指向落账；否则保留并说明缺项。
    for rec in ctx.records:
        for line in rec.lines:
            eff = ctx.effective_line(rec, line)
            if eff.role not in TRANSFER_ROLES:
                continue
            amounts = eff.chosen()
            targets, codes, basis = _targets(ctx, batches, rec, eff)
            # 一张转交凭据覆盖两封时，同样按签收次序与文本指向分；不再要求
            # 凭据本身只指向一户。
            if len([c for c in (eff.covers or []) if c in batches]) >= 2 and amounts:
                hits, problem = allocate_shared(ctx, batches, signed, rec, eff)
                for hit in hits:
                    hit.role = eff.role
                    transfers.append(hit)
                if problem is not None:
                    problem.reasons = sorted(set(problem.reasons + [R_READING]))
                    unallocated.append(problem)
                continue
            if not amounts:
                reasons = _missing_reasons(eff)
                if eff.awaiting or any(k in (eff.text + eff.note) for k in ("另寄", "回单", "未到")):
                    reasons.append(R_AWAITING)
                if not targets:
                    reasons.append(R_HOUSEHOLD)
                unallocated.append(Unresolved(
                    record=rec.id, line=line.id, candidates=eff.candidate_values(),
                    reasons=sorted(set(reasons)), quote=eff.text,
                    household_candidates=targets if len(targets) > 1 else [],
                    note=eff.note or "转交金额尚未能同时确定家户与币种。",
                ))
                continue
            if len(targets) != 1:
                reasons = [R_HOUSEHOLD] + ([R_CURRENCY] if any(a.currency is None for a in amounts) else [])
                unallocated.append(Unresolved(
                    record=rec.id, line=line.id, amounts=amounts,
                    candidates=eff.candidate_values(), reasons=sorted(set(reasons)),
                    household_candidates=targets, quote=eff.text,
                    note=eff.note or "转交金额指向多家或没有指向，不替人选择。",
                ))
                continue
            target = targets[0]
            code = codes[0] if len(codes) == 1 else None
            for amount in amounts:
                if amount.currency is None:
                    unallocated.append(Unresolved(
                        record=rec.id, line=line.id, amounts=[amount],
                        reasons=[R_CURRENCY], quote=eff.text,
                        note="转交金额币种无明确依据，分币种保留，不并入银元。",
                    ))
                    continue
                if code:
                    face = next((a for a in batches[code]["face"] if a.get("currency") == amount.currency), None)
                    already = _settled(code, signed)
                    projected = already.get(amount.currency, 0) + amount.value
                    if face and projected > int(face.get("value", 0)):
                        unallocated.append(Unresolved(
                            record=rec.id, line=line.id, amounts=[amount],
                            reasons=[R_OVER], quote=eff.text,
                            note=(
                                f"若记入 {code}，该批在 {amount.currency} 下将达 {projected}，"
                                f"超过封面 {face['value']}；不把已结批号的款项挪来凑数。"
                            ),
                        ))
                        continue
                transfers.append(Hit(
                    household=target, batch=code, record=rec.id, line=line.id,
                    role=eff.role, amount=amount, quote=eff.text, basis=basis,
                    notes=[eff.note] if eff.note else [],
                ))

    # 第三步：他人代领只记保管，不记家户实收。
    custody_events: list[dict] = []
    for rec in ctx.records:
        for line in rec.lines:
            eff = ctx.effective_line(rec, line)
            if eff.role not in PROXY_ROLES:
                continue
            targets, codes, basis = _targets(ctx, batches, rec, eff)
            amounts = eff.chosen()
            custody_events.append({
                "record": rec.id, "line": line.id, "proxy": eff.proxy,
                "text": eff.text,
                "households": targets or None,
                "batches": codes or None,
                "amounts": [a.as_dict() for a in amounts],
                "basis": basis,
                "note": "代领人领走不等于家户收讫；须另有该户签收或回条。",
            })
            for amount in amounts:
                proxy_pickups.append(Hit(
                    household=targets[0] if len(targets) == 1 else None,
                    batch=codes[0] if len(codes) == 1 else None,
                    record=rec.id, line=line.id, role=eff.role, amount=amount,
                    quote=eff.text, basis=basis,
                    notes=["代领人领走不等于家户收讫；须另有该户签收或回条。"],
                ))
            if len(targets) != 1 and amounts:
                unallocated.append(Unresolved(
                    record=rec.id, line=line.id, amounts=amounts,
                    reasons=[R_HOUSEHOLD], household_candidates=targets,
                    quote=eff.text, note="代领人经手多户，不能因代领人相同而合并家户。",
                ))
            elif len(targets) == 1:
                household_status_notes.setdefault(targets[0], []).append(
                    f"{rec.id} 由 {eff.proxy or '他人'} 代领"
                    + ("（合封全部，金额见各封登记）" if not amounts else "")
                    + "，尚无该户签收。"
                )
            else:
                for target in targets:
                    household_status_notes.setdefault(target, []).append(
                        f"{rec.id} 由 {eff.proxy or '他人'} 经手（可能涉及 {'、'.join(targets)}），"
                        "未指明本户数额，不计本户实收。"
                    )

    # 第四步：退回。
    for rec in ctx.records:
        for line in rec.lines:
            eff = ctx.effective_line(rec, line)
            if eff.role not in RETURN_ROLES:
                continue
            targets, codes, basis = _targets(ctx, batches, rec, eff)
            for amount in eff.chosen():
                returns.append(Hit(
                    household=targets[0] if len(targets) == 1 else None,
                    batch=codes[0] if len(codes) == 1 else None,
                    record=rec.id, line=line.id, role=eff.role, amount=amount,
                    quote=eff.text, basis=basis,
                ))

    # 第五步：批局合封只对总额，不按总额归户。
    consolidations = []
    for rec in ctx.records:
        for line in rec.lines:
            eff = ctx.effective_line(rec, line)
            if eff.role != "consolidation":
                continue
            amounts = eff.chosen()
            covered: list[dict] = []
            cover_codes = eff.covers or [
                part.strip()
                for part in str(eff.batch or "").replace("、", "+").split("+")
            ]
            for code in cover_codes:
                if code in batches:
                    covered.append({"batch": code, **batches[code]})
            face_total: dict[str, int] = {}
            for item in covered:
                for amount in item["face"]:
                    cur = amount.get("currency")
                    face_total[cur] = face_total.get(cur, 0) + int(amount.get("value", 0))
            stated = {a.currency: a.value for a in amounts if a.currency}
            if not amounts:
                unallocated.append(Unresolved(
                    record=rec.id, line=line.id, candidates=eff.candidate_values(),
                    reasons=[R_READING], quote=eff.text,
                    note="合封总额未采用，不能按该额归户。",
                ))
            elif stated and face_total and stated != face_total:
                unallocated.append(Unresolved(
                    record=rec.id, line=line.id, amounts=amounts,
                    reasons=[R_CONFLICT], quote=eff.text,
                    note=f"合封登记 {stated} 与两封封面合计 {face_total} 不一致；不以总额归户，须复核。",
                ))
            consolidations.append({
                "record": rec.id, "line": line.id, "batches": [c["batch"] for c in covered],
                "stated": [a.as_dict() for a in amounts],
                "face_total": {k: {"value": v, "currency": k} for k, v in face_total.items()},
                "pointing": eff.pointing,
                "allocation": eff.allocation,
                "note": eff.note,
            })

    # 第六步：按家户汇总，并把缺口与候选并列。
    households: dict[str, dict] = {}
    for item in corpus.households:
        households[item["id"]] = {
            "id": item["id"], "name": item["name"], "village": item.get("village"),
            "signed": [], "transfers": [], "face": [], "proxy": [], "returned": [],
            "custody": [],
            "status": "no_evidence", "status_notes": [],
            "received_totals": {}, "face_totals": {},
            "open_balance": [], "unresolved": [],
        }
    for hit in signed:
        entry = households[hit.household]
        entry["signed"].append(hit.as_dict())
        cur = hit.amount.currency or "?"
        entry["received_totals"][cur] = entry["received_totals"].get(cur, 0) + hit.amount.value
    for hit in transfers:
        households[hit.household]["transfers"].append(hit.as_dict())
    for hit in proxy_pickups:
        if hit.household:
            households[hit.household]["proxy"].append(hit.as_dict())
    for hit in returns:
        if hit.household:
            households[hit.household]["returned"].append(hit.as_dict())
    for entry in households.values():
        for note in household_status_notes.get(entry["id"], []):
            entry["status_notes"].append(note)

    for batch, info in batches.items():
        if not info["household"]:
            continue
        entry = households[info["household"]]
        for amount in info["face"]:
            item = {"batch": batch, "letter": info["letter"], **amount}
            entry["face"].append(item)
            cur = amount.get("currency") or "?"
            entry["face_totals"][cur] = entry["face_totals"].get(cur, 0) + int(amount.get("value", 0))

    for entry in households.values():
        for event in custody_events:
            if entry["id"] in (event.get("households") or []):
                entry["custody"].append(event)
        for cur, face in entry["face_totals"].items():
            got = entry["received_totals"].get(cur, 0)
            if got < face:
                entry["open_balance"].append({
                    "currency": cur, "value": face - got,
                    "note": "封面额与家户已签收额的差额；差额不是已收款，须有签收或回单才能入账。",
                })
            elif got > face:
                entry["open_balance"].append({
                    "currency": cur, "value": got - face,
                    "note": "家户签收超过封面额，须复核是否同一批或重复抄录。",
                })
        entry["status"] = _household_status(entry)

    # 未分款项按家户候选归类，缺什么写什么。
    for item in unallocated:
        for hh in item.household_candidates:
            if hh in households:
                households[hh]["unresolved"].append(item.as_dict())

    batches_out = {}
    for batch, info in batches.items():
        signed_here = [h for h in signed if h.batch == batch]
        batches_out[batch] = {
            **info,
            "signed": [h.as_dict() for h in signed_here],
            "signed_totals": _settled(batch, signed),
            "status": "signed" if signed_here else "no_signoff",
        }

    return {
        "gate": ctx.gate,
        "households": households,
        "batches": batches_out,
        "consolidations": consolidations,
        "unallocated": [u.as_dict() for u in unallocated],
        "transfers": [t.as_dict() for t in transfers],
        "proxy_pickups": [p.as_dict() for p in proxy_pickups],
        "returns": [r.as_dict() for r in returns],
        "custody_events": custody_events,
        "readings_pending": readings_pending,
        "revisions_applied": [r.id for r in ctx.revisions],
        "reopen_impact": reopen_impact(ctx, {
            "batches": batches_out,
            "households": households,
        }),
    }


def signoff_sequence(book: dict, household: str | None = None) -> list[dict]:
    """按签收次序列出已签收的金额，便复核同一批的多次签收。"""

    rows = []
    for hid, entry in book["households"].items():
        if household is not None and hid != household:
            continue
        for hit in entry["signed"]:
            rows.append({
                "household": hid,
                "sealed": hit["batch"],
                "sequence": hit["sequence"],
                "record": hit["record"],
                "line": hit["line"],
                "amount": hit["amount"],
                "quote": hit["quote"],
            })
    rows.sort(key=lambda r: (r["sealed"] or "", r["sequence"], r["record"], r["line"]))
    return rows


def _household_status(entry: dict) -> str:
    if entry["returned"] and not entry["signed"]:
        return "returned"
    if entry["signed"]:
        if entry["open_balance"] and all(item["value"] == 0 for item in entry["open_balance"]):
            return "signed"
        if any(item["note"].startswith("封面额与家户已签收额的差额") and item["value"] > 0 for item in entry["open_balance"]):
            return "signed_partial"
        return "signed"
    if entry["proxy"] or entry["custody"]:
        return "proxy_unverified"
    if entry["transfers"]:
        return "forwarded"
    if entry["face"]:
        return "no_signoff"
    return "no_evidence"


def reopen_impact(ctx: EvidenceContext, book: dict) -> list[dict]:
    """复核重开某行时，列出必须一并回看的共用凭据与已分金额。

    重开只说明“这一行的金额暂不采用”，不等于改判；这里不改账，只把共用
    收据的其他批号、那些批号已分出的金额和凭据摆在一起，供复核判断。
    若另一家的款项与来源确与本行无关，其已入账金额继续有效。
    """

    rows = []
    batches = book.get("batches", {})
    for revision in ctx.revisions:
        payload = revision.revision or {}
        for item in payload.get("reopen") or []:
            line_id = str(item.get("line", ""))
            if ":" not in line_id:
                continue
            record_id = line_id.split(":", 1)[0]
            record = ctx.corpus.by_id(record_id)
            line = record.line(line_id)
            parties = resolve_party(ctx.corpus, record, line)
            own_batches: list[str] = []
            for other in record.lines:
                for code in (other.sealed_reference, other.batch):
                    if code and code in batches:
                        for part in str(code).replace("、", "+").split("+"):
                            part = part.strip()
                            if part and part not in own_batches:
                                own_batches.append(part)
            shared = []
            related_batches: list[str] = []
            for other in ctx.records:
                for ln in other.lines:
                    if ln.role != "consolidation":
                        continue
                    covers = list(ln.covers)
                    if not set(covers) & set(own_batches):
                        continue
                    shared.append({
                        "record": other.id, "line": ln.id,
                        "covers": covers, "pointing": ln.pointing,
                        "text": ln.text, "same_as": ln.same_as,
                    })
                    for code in covers:
                        if code not in own_batches and code not in related_batches:
                            related_batches.append(code)
            others = []
            for code in related_batches:
                info = batches.get(code, {})
                hits = info.get("signed") or []
                others.append({
                    "batch": code,
                    "household": info.get("household"),
                    "signed_totals": info.get("signed_totals") or {},
                    "evidence": [f"{hit['record']}:{hit['line']}" for hit in hits],
                    "sources": sorted({hit["record"] for hit in hits}),
                })
            reopen_sources = {
                src for entry in others for src in entry["sources"]
            }
            own_sources = set()
            for code in own_batches:
                info = batches.get(code, {})
                own_sources.update(hit["record"] for hit in info.get("signed") or [])
            own_sources.add(record.id)
            rows.append({
                "revision": revision.id,
                "line": line_id,
                "household": parties.household,
                "household_candidates": parties.candidates,
                "own_batches": own_batches,
                "field": item.get("field"),
                "to": item.get("to"),
                "note": item.get("note", ""),
                "shared_receipts": shared,
                "other_batches": others,
                "other_sources_independent": not (own_sources & reopen_sources),
                "own_sources": sorted(own_sources),
                "revision_note": payload.get("reopen_note", ""),
            })
    return rows


def triage(book: dict) -> list[dict]:
    """把未决款项按“缺家户、缺币种、等回单、读法未定、冲突”分类。"""

    rows = []
    for item in book["unallocated"]:
        reasons = item["reasons"]
        labels = {
            R_HOUSEHOLD: "缺家户证据",
            R_CURRENCY: "缺币种依据",
            R_READING: "读法未定",
            R_AWAITING: "等另一张回单",
            R_CONFLICT: "证据冲突",
            R_REOPEN: "复核已重开、待重新判定",
            R_OVER: "超过封面/已结，不能挪用",
        }
        rows.append({
            "record": item["record"], "line": item["line"],
            "missing": [labels.get(r, r) for r in reasons],
            "candidates": item["candidates"],
            "amounts": item["amounts"],
            "household_candidates": item["household_candidates"],
            "quote": item["quote"], "note": item["note"],
        })
    return rows


def household_statement(book: dict, household_id: str) -> dict:
    if household_id not in book["households"]:
        raise KeyError(f"账本中没有家户 {household_id!r}")
    entry = book["households"][household_id]
    lines = []
    for hit in entry["signed"]:
        lines.append({
            "kind": "signed",
            "amount": hit["amount"],
            "record": hit["record"], "line": hit["line"],
            "quote": hit["quote"],
            "currency_basis": hit["currency_basis"],
            "sequence": hit["sequence"],
            "basis": hit["basis"],
            "notes": hit["notes"],
        })
    for hit in entry["transfers"]:
        lines.append({
            "kind": "transfer",
            "amount": hit["amount"],
            "record": hit["record"], "line": hit["line"],
            "quote": hit["quote"], "currency_basis": hit["currency_basis"],
            "basis": hit["basis"], "notes": hit["notes"],
        })
    return {
        "household": household_id,
        "name": entry["name"],
        "status": entry["status"],
        "received_totals": entry["received_totals"],
        "face_totals": entry["face_totals"],
        "open_balance": entry["open_balance"],
        "lines": lines,
        "custody": entry.get("custody", []),
        "unresolved": entry["unresolved"],
        "status_notes": entry["status_notes"],
    }
