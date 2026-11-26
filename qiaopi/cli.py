"""命令行入口。

    python3 -m qiaopi check                     # 资料自检
    python3 -m qiaopi book --gate 9             # 截至第几步可见的证据的分账摘要
    python3 -m qiaopi show H1                   # 一户的逐笔来款与差额
    python3 -m qiaopi line L1 L1:4              # 一行的读法、候选与归户依据
    python3 -m qiaopi ledger                    # 旧账页与当前分账的差异
    python3 -m qiaopi export out/               # 写出报告、CSV 与账页转写
"""
from __future__ import annotations

import argparse
import json
import sys

from .allocator import settle
from .evidence import EvidenceContext
from .ledger import cited_claims_changed, reconcile, render_page
from .model import load_corpus
from .report import explain_line, statement_text, summarize, write_all
from .validate import has_errors, validate


def _load(args):
    return load_corpus(getattr(args, "corpus", None))


def _gate(args) -> int | None:
    return getattr(args, "gate", None)


def cmd_check(args) -> int:
    corpus = _load(args)
    findings = validate(corpus)
    if not findings:
        print("资料自检通过：没有结构错误，也没有需要复核的警示。")
        return 0
    for item in findings:
        print(f"[{item.level}] {item.code} {item.where}：{item.message}")
    return 1 if has_errors(findings) else 0


def cmd_book(args) -> int:
    corpus = _load(args)
    book = settle(EvidenceContext.build(corpus, _gate(args)))
    if args.json:
        print(json.dumps(book, ensure_ascii=False, indent=2))
    else:
        print(summarize(book))
    return 0


def cmd_show(args) -> int:
    corpus = _load(args)
    book = settle(EvidenceContext.build(corpus, _gate(args)))
    print(statement_text(book, corpus, args.household))
    return 0


def cmd_line(args) -> int:
    corpus = _load(args)
    print(explain_line(corpus, args.record, args.line_id))
    return 0


def cmd_ledger(args) -> int:
    corpus = _load(args)
    book = settle(EvidenceContext.build(corpus, _gate(args)))
    report = reconcile(corpus, book)
    if args.page:
        print(render_page(report, args.page))
        return 0
    for version in report["versions"]:
        print(f"版本 {version['version']}（{version['period']}）")
        for page in version["pages"]:
            print(render_page(report, page["locator"]))
            print()
    changes = cited_claims_changed(report)
    if changes:
        print("引用所凭分账已变化：")
        for row in changes:
            print(f"  {row['locator']} {row['sealed']}（{row['household']}），引用者 {'、'.join(row['cited_by'] or ['—'])}")
    return 0


def cmd_reopen(args) -> int:
    corpus = _load(args)
    book = settle(EvidenceContext.build(corpus, _gate(args)))
    rows = book.get("reopen_impact") or []
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("没有待复核的重开行。")
        return 0
    for row in rows:
        print(f"{row['revision']} 重开 {row['line']}（{row['household'] or '户属未定'}；{row['field']} → {row['to']}）")
        print(f"  本行所在批号：{'、'.join(row['own_batches']) or '—'}")
        for receipt in row["shared_receipts"]:
            print(f"  共用凭据 {receipt['record']}:{receipt['line']} 覆盖 {'、'.join(receipt['covers'])}")
            if receipt.get("pointing"):
                print(f"    指向：{receipt['pointing']}")
        for other in row["other_batches"]:
            totals = "、".join(f"{k}{v}" for k, v in (other["signed_totals"] or {}).items()) or "无签收"
            print(f"  另一批 {other['batch']}（{other['household']}）：已签收 {totals}；凭 {'、'.join(other['evidence']) or '—'}")
        if row["note"]:
            print(f"  重开说明：{row['note']}")
        if row["revision_note"]:
            print(f"  复核注：{row['revision_note']}")
        print("  另一家来源独立，仍可入账。" if row["other_sources_independent"]
              else "  另一家款项与本行来源相关，须一并复看。")
    return 0


def cmd_export(args) -> int:
    corpus = _load(args)
    book = settle(EvidenceContext.build(corpus, _gate(args)))
    paths = write_all(args.outdir, book, corpus)
    for name, path in paths.items():
        print(f"{name}\t{path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qiaopi", description="侨批银款核账：从转写到可分户追查的账。")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--corpus", default=argparse.SUPPRESS, help="corpus.json 路径")
    common.add_argument("--gate", type=int, default=argparse.SUPPRESS, help="只用第几步及以前可见的证据")
    parser.add_argument("--corpus", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--gate", type=int, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", parents=[common], help="资料与规则自检")
    check.set_defaults(func=cmd_check)

    book = sub.add_parser("book", parents=[common], help="分户分账摘要")
    book.add_argument("--json", action="store_true")
    book.set_defaults(func=cmd_book)

    show = sub.add_parser("show", parents=[common], help="一户的来款说明")
    show.add_argument("household")
    show.set_defaults(func=cmd_show)

    line = sub.add_parser("line", parents=[common], help="一行的读法与依据")
    line.add_argument("record")
    line.add_argument("line_id")
    line.set_defaults(func=cmd_line)

    ledger = sub.add_parser("ledger", parents=[common], help="账页对照")
    ledger.add_argument("--page", default=None, help="如 1923-09/T08")
    ledger.set_defaults(func=cmd_ledger)

    reopen = sub.add_parser("reopen", parents=[common], help="复核重开一行时须回看的共用凭据")
    reopen.add_argument("--json", action="store_true")
    reopen.set_defaults(func=cmd_reopen)

    export = sub.add_parser("export", parents=[common], help="导出报告")
    export.add_argument("outdir")
    export.set_defaults(func=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
