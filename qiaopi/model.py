"""核账资料的数据模型。

金额一律以最小单位的整数保存，并连同来源币种一起保存；不做任何跨币种
折算。凡转写不明确之处（十／廿、年份、金额后的“角／元”等）都保留原文
候选，由依据说明采用或未采用的原因。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent

KINDS = ("家户", "银信", "交接", "收据", "引用", "复核")

# 家户实收/款项去向的结果状态（核账输出用；具体取值见 allocator）。
STATUSES = (
    "signed",            # 家户收讫：有指向该户的签收
    "signed_partial",    # 部分到款，余数未见签收
    "forwarded",         # 转交：已交他人/批局转递，尚未见家户签收
    "proxy_unverified",  # 亲友代领：有人代领，但无该户签收
    "no_signoff",        # 有寄出记录，未见任何签收
    "returned",          # 批局退回
    "no_evidence",       # 无到款证据
)

# 行在业务中的角色。
ROLES = (
    "face",            # 封面的汇寄金额
    "use_intent",      # 信中嘱托的用钱意向
    "old_debt",        # 旧欠（不并入本次汇寄）
    "instruction",     # 交收指示（不直接是金额）
    "consolidation",   # 批局合封登记
    "custody",         # 保管/转递
    "proxy_pickup",    # 他人代领
    "forward",         # 转交一笔金额
    "stub",            # 底联
    "signoff",         # 家户签收
    "outstanding_note",  # 关于余款/未到的说明
    "seal",            # 封背/封口批注
    "registration_note",  # 登记栏说明（如“同上”指哪一栏）
    "return",          # 退回
    "memo",            # 其他说明
)


@dataclass(frozen=True)
class Amount:
    """带来源币种的最小单位整数金额。currency=None 表示币种无明确依据。"""

    value: int
    currency: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError("金额不能为负；方向由记录语义表达")
        if self.currency is not None:
            object.__setattr__(self, "currency", self.currency.strip().upper())

    def as_dict(self) -> dict:
        return {"value": self.value, "currency": self.currency, "note": self.note}


@dataclass
class Basis:
    """一条核账判断的依据：读哪条记录、哪一行、哪句原文。"""

    record: str
    line: str
    quote: str
    evidence: str
    rule: str = ""
    alternatives: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "record": self.record,
            "line": self.line,
            "quote": self.quote,
            "evidence": self.evidence,
            "rule": self.rule,
            "alternatives": list(self.alternatives),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Basis":
        return cls(
            record=payload.get("record", ""),
            line=payload.get("line", ""),
            quote=payload.get("quote", ""),
            evidence=payload.get("evidence", ""),
            rule=payload.get("rule", ""),
            alternatives=list(payload.get("alternatives", [])),
        )


@dataclass
class Line:
    """记录中的一行读文。

    ``amount_candidates`` 保留笔迹上的全部候选（含断行、十／廿、角／元）；
    ``amounts`` 是核账后采用的分币种金额。候选有多个且无法判别时，
    ``amounts`` 必须为空，不能挑一个刚好对平的值。
    """

    id: str
    role: str
    text: str
    addressee: str | None = None
    signature: str | None = None
    currency: str | None = None
    unit: str | None = None
    amount_candidates: list[dict] = field(default_factory=list)
    amounts: list[dict] = field(default_factory=list)
    purpose: str | None = None
    sealed_reference: str | None = None
    batch: str | None = None
    covers: list[str] = field(default_factory=list)
    pointing: str | None = None
    allocation: dict | None = None
    proxy: str | None = None
    same_as: dict | None = None
    genuine_delivery: bool | None = None
    awaiting: bool = False
    reopened_by_review: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"行 {self.id}: 未知角色 {self.role!r}")
        if self.currency is not None:
            self.currency = self.currency.strip().upper()

    def chosen(self) -> list[Amount]:
        return [Amount(int(a["value"]), a.get("currency"), a.get("note", "")) for a in self.amounts]

    def candidate_values(self) -> list[dict]:
        return list(self.amount_candidates)

    @classmethod
    def from_dict(cls, payload: dict) -> "Line":
        return cls(
            id=str(payload["id"]),
            role=str(payload["role"]),
            text=str(payload.get("text", "")),
            addressee=payload.get("addressee"),
            signature=payload.get("signature"),
            currency=payload.get("currency"),
            unit=payload.get("unit"),
            amount_candidates=list(payload.get("amount_candidates") or []),
            amounts=list(payload.get("amounts") or []),
            purpose=payload.get("purpose"),
            sealed_reference=payload.get("sealed_reference"),
            batch=payload.get("batch"),
            covers=list(payload.get("covers") or []),
            pointing=payload.get("pointing"),
            allocation=dict(payload["allocation"]) if payload.get("allocation") else None,
            proxy=payload.get("proxy"),
            same_as=dict(payload["same_as"]) if payload.get("same_as") else None,
            genuine_delivery=payload.get("genuine_delivery"),
            awaiting=bool(payload.get("awaiting", False)),
            reopened_by_review=bool(payload.get("reopened_by_review", False)),
            note=str(payload.get("note", "")),
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "text": self.text,
            "addressee": self.addressee,
            "signature": self.signature,
            "currency": self.currency,
            "unit": self.unit,
            "amount_candidates": list(self.amount_candidates),
            "amounts": list(self.amounts),
            "purpose": self.purpose,
            "sealed_reference": self.sealed_reference,
            "batch": self.batch,
            "covers": list(self.covers),
            "pointing": self.pointing,
            "allocation": self.allocation,
            "proxy": self.proxy,
            "same_as": self.same_as,
            "genuine_delivery": self.genuine_delivery,
            "awaiting": self.awaiting,
            "reopened_by_review": self.reopened_by_review,
            "note": self.note,
        }


@dataclass
class Record:
    id: str
    kind: str
    text: str
    references: list[str] = field(default_factory=list)
    arrival_order: int = 0
    revealed_at: int = 1
    authenticity: str = "original"
    copy_of: str | None = None
    protocol: str | None = None
    approximate_period: str | None = None
    lines: list[Line] = field(default_factory=list)
    party: str | None = None
    party_basis: list[dict] = field(default_factory=list)
    custody: dict = field(default_factory=dict)
    year_candidates: list[dict] = field(default_factory=list)
    true_month: int | None = None
    citation: dict | None = None
    revision: dict | None = None
    note: str = ""

    @property
    def is_revision(self) -> bool:
        return self.kind == "复核" or self.authenticity == "revision"

    @property
    def date(self) -> dict:
        return {
            "year_candidates": list(self.year_candidates),
            "true_month": self.true_month,
            "approximate": self.approximate_period,
        }

    def line(self, line_id: str) -> Line:
        for item in self.lines:
            if item.id == line_id:
                return item
        raise KeyError(f"{self.id}: 没有行 {line_id!r}")

    @classmethod
    def from_dict(cls, payload: dict) -> "Record":
        rec = cls(
            id=str(payload["id"]),
            kind=str(payload["kind"]),
            text=str(payload.get("text", "")),
            references=list(payload.get("references") or []),
            arrival_order=int(payload.get("arrival_order", 0)),
            revealed_at=int(payload.get("revealed_at", 1)),
            authenticity=str(payload.get("authenticity", "original")),
            copy_of=payload.get("copy_of"),
            protocol=payload.get("protocol"),
            approximate_period=payload.get("approximate_period"),
            lines=[Line.from_dict(item) for item in payload.get("lines") or []],
            party=payload.get("party"),
            party_basis=[Basis.from_dict(b).as_dict() for b in payload.get("party_basis") or []],
            custody=dict(payload.get("custody") or {}),
            year_candidates=list(payload.get("year_candidates") or []),
            true_month=payload.get("true_month"),
            citation=dict(payload["citation"]) if payload.get("citation") else None,
            revision=dict(payload["revision"]) if payload.get("revision") else None,
            note=str(payload.get("note", "")),
        )
        if rec.kind not in KINDS:
            raise ValueError(f"{rec.id}: 未知记录类型 {rec.kind!r}")
        ids = [line.id for line in rec.lines]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{rec.id}: 行号重复")
        return rec

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "references": list(self.references),
            "arrival_order": self.arrival_order,
            "revealed_at": self.revealed_at,
            "authenticity": self.authenticity,
            "copy_of": self.copy_of,
            "protocol": self.protocol,
            "approximate_period": self.approximate_period,
            "lines": [line.as_dict() for line in self.lines],
            "party": self.party,
            "party_basis": list(self.party_basis),
            "custody": dict(self.custody),
            "year_candidates": list(self.year_candidates),
            "true_month": self.true_month,
            "citation": self.citation,
            "revision": self.revision,
            "note": self.note,
        }


@dataclass
class LedgerVersion:
    """批局按月账页的一个版本。已刊页一经引用即不可改写。"""

    version: str
    period: str = ""
    pages: dict[str, dict] = field(default_factory=dict)
    supersedes: str | None = None
    revealed_at: int = 1
    note: str = ""

    @classmethod
    def from_dict(cls, payload: dict) -> "LedgerVersion":
        return cls(
            version=str(payload["version"]),
            period=str(payload.get("period", "")),
            pages=dict(payload.get("pages") or {}),
            supersedes=payload.get("supersedes"),
            revealed_at=int(payload.get("revealed_at", 1)),
            note=str(payload.get("note", "")),
        )

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "period": self.period,
            "pages": json.loads(json.dumps(self.pages, ensure_ascii=False)),
            "supersedes": self.supersedes,
            "revealed_at": self.revealed_at,
            "note": self.note,
        }


@dataclass
class Corpus:
    records: list[Record]
    households: list[dict] = field(default_factory=list)
    ledger_versions: list[LedgerVersion] = field(default_factory=list)
    currency_units: dict[str, int] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def by_id(self, record_id: str) -> Record:
        found = [r for r in self.records if r.id == record_id]
        if not found:
            raise KeyError(f"没有记录 {record_id!r}")
        return found[0]

    def ids(self) -> list[str]:
        return [r.id for r in self.records]

    def household(self, household_id: str) -> dict:
        for item in self.households:
            if item["id"] == household_id:
                return item
        raise KeyError(f"没有家户 {household_id!r}")

    @property
    def ledger(self) -> dict[str, LedgerVersion]:
        return {v.version: v for v in self.ledger_versions}

    def unit(self, currency: str | None) -> int | None:
        if currency is None:
            return None
        return self.currency_units.get(currency)

    def records_up_to(self, gate: int) -> list[Record]:
        return [r for r in self.records if r.revealed_at <= gate]

    @classmethod
    def from_dict(cls, payload: dict) -> "Corpus":
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            raise ValueError("corpus 缺少 records 列表")
        records = [Record.from_dict(item) for item in raw_records]
        return cls(
            records=records,
            households=[dict(h) for h in payload.get("households") or []],
            ledger_versions=[LedgerVersion.from_dict(v) for v in payload.get("ledger_versions") or []],
            currency_units={k.upper(): int(v) for k, v in (payload.get("currency_units") or {}).items()},
            meta={k: v for k, v in payload.items()
                  if k not in {"records", "households", "ledger_versions", "currency_units"}},
        )


def load_corpus(path: str | Path | None = None) -> Corpus:
    path = Path(path) if path is not None else ROOT / "data" / "corpus.json"
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return Corpus.from_dict(payload)


def from_records(records: Iterable[dict], **context) -> Corpus:
    """由最小记录列表构造 Corpus，便于补录复核或测试。"""

    return Corpus.from_dict({"records": list(records), **context})


def dump_corpus(corpus: Corpus, path: str | Path) -> None:
    payload = {
        **corpus.meta,
        "currency_units": dict(corpus.currency_units),
        "households": [dict(h) for h in corpus.households],
        "ledger_versions": [v.as_dict() for v in corpus.ledger_versions],
        "records": [r.as_dict() for r in corpus.records],
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100}
_FINANCIAL = {"壹": 1, "贰": 2, "貳": 2, "叁": 3, "參": 3, "肆": 4, "伍": 5,
              "陆": 6, "陸": 6, "柒": 7, "捌": 8, "玖": 9}


def parse_number(token: str) -> int | None:
    """把中文或阿拉伯数字读成整数；读不清时返回 None。

    支持“十五”“廿”“廿五”“四十五”“百二十”“壹拾伍”“15”等写法。
    只做数字解析，不猜测残缺字；也不把“廿”误读成“二十”以外的值。
    """

    token = (token or "").strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    if token.startswith("廿"):
        rest = parse_number(token[1:]) if len(token) > 1 else 0
        return 20 + (rest or 0) if rest is not None else None
    if token.startswith("卅"):
        rest = parse_number(token[1:]) if len(token) > 1 else 0
        return 30 + (rest or 0) if rest is not None else None
    total = 0
    num = None
    for ch in token:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _FINANCIAL:
            num = _FINANCIAL[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if num is None:
                num = 1
            total += num * unit
            num = None
        else:
            return None
    if num is not None:
        total += num
    return total or None


_NUM = r"[零〇一二两三四五六七八九十拾百佰廿卅壹贰貳叁參肆伍陆陸柒捌玖\d]+"
_MONEY_RE = re.compile(
    # “银元贰拾”“大银一十五元”“银五元”
    rf"(?:(?P<prefix>大银|银元|银)\s*(?P<num0>{_NUM})(?:\s*(?P<unit0>银元|银|元))?"
    # “十五元”“五角”“伍分”
    rf"|(?P<num1>{_NUM})\s*(?P<unit1>银元|银|元|角|分))"
)


def parse_money(text: str) -> list[dict]:
    """从文字中按出现顺序取出金额，保留币种写法。

    返回 ``[{"number":15,"unit":"元","value":1500,"currency":"银元","raw":"十五元"}]``。
    只有同一分句里写明“银/银元/大银”时才记银元；“元”“角”“分”单独出现时
    币种留空，由调用方结合上下文判断，不默认折成银元。
    """

    found = []
    for match in _MONEY_RE.finditer(text or ""):
        number = parse_number(match.group("num0") or match.group("num1") or "")
        if number is None:
            continue
        unit = match.group("unit0") or match.group("unit1") or "元"
        if unit in ("角",):
            value = number * 10
        elif unit in ("分",):
            value = number
        else:
            value = number * 100
        prefix = match.group("prefix")
        if unit in ("银元", "银") or prefix in ("大银", "银元", "银"):
            currency = "银元"
        else:
            currency = None
        found.append({
            "number": number,
            "unit": unit,
            "prefix": prefix,
            "value": value,
            "currency": currency,
            "raw": match.group(0),
        })
    return found


def clean_text(text: str) -> str:
    """供报告使用的原文清理：只折叠空白，不改字。"""

    return re.sub(r"\s+", " ", text or "").strip()


def format_amount(value: int | None, currency: str | None) -> str:
    """把最小单位整数显示为便于校读的金额；币种未定时不替它补。"""

    if value is None:
        return "—"
    if currency == "银元":
        yuan, fen = divmod(int(value), 100)
        return f"银元{yuan}" + (f"又{fen:02d}分" if fen else "")
    if currency:
        whole, minor = divmod(int(value), 100)
        if minor:
            return f"{whole}.{minor:02d}（{currency}）"
        return f"{whole}（{currency}）"
    return f"{value}（币种未定）"


__all__ = [
    "Amount", "Basis", "Corpus", "KINDS", "LedgerVersion", "Line", "ROLES", "Record",
    "STATUSES", "clean_text", "dump_corpus", "format_amount", "from_records",
    "load_corpus", "parse_money", "parse_number",
]
