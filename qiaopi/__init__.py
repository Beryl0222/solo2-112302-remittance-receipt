"""侨批银款核账。

从批封、信纸、批局登记与收据的转写出发，做可反复运行、可逐行追查的
家户来款与未对平账目。用法见 ``python3 -m qiaopi --help``。
"""
from .allocator import household_statement, readings_pending, reopen_impact, settle, triage
from .evidence import EvidenceContext, resolve_party
from .ledger import cited_claims_changed, reconcile, render_page
from .model import Basis, Corpus, Line, Record, load_corpus, parse_money, parse_number
from .report import document, explain_line, statement_text, summarize, write_all
from .validate import validate

__all__ = [
    "Basis", "Corpus", "EvidenceContext", "Line", "Record",
    "cited_claims_changed", "document", "explain_line", "household_statement",
    "load_corpus", "parse_money", "parse_number", "readings_pending", "reconcile",
    "render_page", "reopen_impact",
    "resolve_party", "settle", "statement_text", "summarize", "triage",
    "validate", "write_all",
]
