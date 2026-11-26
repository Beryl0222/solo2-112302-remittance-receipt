"""端到端场景测试：同一批资料按渐次出现的证据重排。

这些测试锁定业务规则，不锁定某一版的账页数字。
"""
from __future__ import annotations

import copy
import unittest

from qiaopi.allocator import household_statement, settle, triage
from qiaopi.evidence import EvidenceContext, resolve_party
from qiaopi.ledger import cited_claims_changed, reconcile
from qiaopi.model import Corpus, from_records, load_corpus, parse_money, parse_number
from qiaopi.report import explain_line
from qiaopi.validate import has_errors, validate


def book_at(gate: int) -> dict:
    return settle(EvidenceContext.build(load_corpus(), gate))


class OpeningReadingTest(unittest.TestCase):
    """“十五元，前欠五元照旧”的两种读法都要保留。"""

    def test_face_amount_is_sent_not_received(self):
        book = book_at(11)
        # 封面十五元只是寄出额；家户实收以 R1 的十元为准。
        self.assertEqual(book["households"]["H1"]["received_totals"], {"银元": 1000})
        self.assertEqual(book["households"]["H1"]["face_totals"], {"银元": 1500})

    def test_old_debt_not_used_to_close_gap(self):
        corpus = load_corpus()
        book = book_at(11)
        h1 = book["households"]["H1"]
        self.assertEqual(h1["open_balance"][0]["value"], 500)
        line = corpus.by_id("L1").line("L1:4")
        readings = {c["value"] for c in line.amount_candidates}
        self.assertEqual(readings, {1500, 1000})
        self.assertEqual(line.amounts, [], "两种读法未判别前不得采用某一值")

    def test_exhibit_label_not_evidence(self):
        corpus = load_corpus()
        text = explain_line(corpus, "L1", "L1:1")
        self.assertIn("展签", text)
        self.assertIn("不等于家户实收", text)

    def test_number_candidates_preserved(self):
        self.assertEqual(parse_number("十五"), 15)
        self.assertEqual(parse_number("廿五"), 25)
        self.assertIsNone(parse_number("十五／"))
        money = parse_money("大银一十五元，五角")
        self.assertEqual(money[0]["value"], 1500)
        self.assertEqual(money[0]["currency"], "银元")
        # 只写“五角”而没有币种依据时，不默认折成银元。
        self.assertIsNone(money[1]["currency"])


class SameAsAndBreakTest(unittest.TestCase):
    def test_same_as_points_to_handler_column_not_amount(self):
        book = book_at(11)
        # 登记末列“同上”回指经手人，不能把两封金额相加或合并。
        self.assertEqual(len(book["consolidations"]), 1)
        codes = book["consolidations"][0]["batches"]
        self.assertEqual(sorted(codes), ["乙二", "甲七"])
        self.assertEqual(book["consolidations"][0]["face_total"]["银元"]["value"], 2000)

    def test_broken_line_keeps_currency_unknown(self):
        book = book_at(11)
        pending = [u for u in triage(book) if u["record"] == "P2"]
        self.assertEqual(len(pending), 1)
        self.assertIn("缺币种依据", pending[0]["missing"])
        self.assertTrue(all(c["currency"] is None for c in pending[0]["candidates"]))
        self.assertEqual(book["households"]["H1"]["received_totals"], {"银元": 1000})

    def test_currency_not_folded(self):
        corpus = load_corpus()
        units = corpus.currency_units
        self.assertEqual(units["银元"], 100)


class EvidenceProgressionTest(unittest.TestCase):
    def test_second_receipt_changes_only_h2(self):
        before = book_at(8)
        after = book_at(9)
        # 初四只有林禾代领：H2 停在“他人代领，无家户签收”。
        self.assertEqual(before["households"]["H2"]["status"], "proxy_unverified")
        self.assertEqual(before["households"]["H2"]["received_totals"], {})
        self.assertEqual(after["households"]["H2"]["status"], "signed")
        self.assertEqual(after["households"]["H2"]["received_totals"], {"银元": 500})
        # H1 的未到五元不受 H2 新回条影响。
        self.assertEqual(before["households"]["H1"]["received_totals"], after["households"]["H1"]["received_totals"])

    def test_gate_hides_later_evidence(self):
        early = book_at(8)
        self.assertNotIn("R2", [hit["record"] for hit in early["households"]["H2"]["signed"]])
        late = book_at(11)
        self.assertIn("R2", [hit["record"] for hit in late["households"]["H2"]["signed"]])


class HouseholdResolutionTest(unittest.TestCase):
    def test_single_character_alias_needs_village(self):
        corpus = load_corpus()
        p1 = corpus.by_id("P1")
        resolution = resolve_party(corpus, p1, p1.line("P1:2"))
        self.assertIsNone(resolution.household)
        self.assertEqual(resolution.candidates, ["H1", "H2"])

    def test_r2_alias_resolved_by_village(self):
        corpus = load_corpus()
        r2 = corpus.by_id("R2")
        resolution = resolve_party(corpus, r2, r2.line("R2:1"))
        self.assertEqual(resolution.household, "H2")
        self.assertIn("街口", resolution.basis[0].quote)

    def test_same_proxy_does_not_merge_households(self):
        book = book_at(11)
        self.assertEqual(len(book["households"]), 2)
        self.assertEqual(book["households"]["H1"]["received_totals"], {"银元": 1000})
        self.assertEqual(book["households"]["H2"]["received_totals"], {"银元": 500})

    def test_proxy_pickup_not_counted_as_received(self):
        book = book_at(11)
        events = book["custody_events"]
        self.assertTrue(events)
        # 代领记录本身不带金额（合封全部），家户实收只能来自签收。
        self.assertEqual(events[0]["record"], "P1")
        self.assertTrue(book["households"]["H1"]["proxy"] or book["households"]["H2"]["proxy"]
                        or "P1" in " ".join(book["households"]["H1"]["status_notes"]
                                            + book["households"]["H2"]["status_notes"]))
        for hit in book["households"]["H1"]["signed"]:
            self.assertNotEqual(hit["record"], "P1")
        for hit in book["households"]["H2"]["signed"]:
            self.assertNotEqual(hit["record"], "P1")


class OutcomesTest(unittest.TestCase):
    """寄交、转交、家户收讫、批局退回、亲友代领要能分开。"""

    def _corpus(self, extra_lines):
        records = [
            {"id": "L1", "kind": "银信", "text": "封标甲七。大银五元。", "arrival_order": 1,
             "party": "H1", "lines": [
                 {"id": "L1:1", "role": "face", "text": "封标甲七。大银五元。",
                  "sealed_reference": "甲七",
                  "amounts": [{"value": 500, "currency": "银元"}]}]},
            {"id": "T1", "kind": "交接", "text": "转交", "arrival_order": 2, "lines": extra_lines},
        ]
        return from_records(records, households=[{"id": "H1", "name": "甲家"}])

    def test_forwarded_is_not_received(self):
        corpus = self._corpus([
            {"id": "T1:1", "role": "forward", "text": "批局转交五元",
             "sealed_reference": "甲七", "pointing": "甲七",
             "amounts": [{"value": 500, "currency": "银元"}]},
        ])
        book = settle(EvidenceContext.build(corpus))
        self.assertEqual(book["households"]["H1"]["status"], "forwarded")
        self.assertEqual(book["households"]["H1"]["received_totals"], {})

    def test_returned_is_not_received(self):
        corpus = self._corpus([
            {"id": "T1:1", "role": "return", "text": "批局退回五元",
             "sealed_reference": "甲七",
             "amounts": [{"value": 500, "currency": "银元"}]},
        ])
        book = settle(EvidenceContext.build(corpus))
        self.assertEqual(book["households"]["H1"]["status"], "returned")
        self.assertEqual(book["households"]["H1"]["received_totals"], {})

    def test_forward_without_currency_stays_separate(self):
        corpus = self._corpus([
            {"id": "T1:1", "role": "forward", "text": "转交五",
             "sealed_reference": "甲七", "pointing": "甲七",
             "amounts": [{"value": 500, "currency": None}]},
        ])
        book = settle(EvidenceContext.build(corpus))
        self.assertEqual(book["households"]["H1"]["received_totals"], {})
        pending = [u for u in book["unallocated"] if u["record"] == "T1"]
        self.assertEqual(len(pending), 1)
        self.assertIn("missing_currency", pending[0]["reasons"])
        self.assertNotIn("银元", {a["currency"] for a in pending[0]["amounts"] if a["currency"]})

    def test_forward_awaiting_receipt_is_marked(self):
        corpus = self._corpus([
            {"id": "T1:1", "role": "forward", "text": "另寄，待回单",
             "sealed_reference": "甲七", "pointing": "甲七", "awaiting": True,
             "amount_candidates": [{"value": 500, "currency": None, "reading": "五"}]},
        ])
        book = settle(EvidenceContext.build(corpus))
        pending = [u for u in book["unallocated"] if u["record"] == "T1"]
        self.assertEqual(len(pending), 1)
        self.assertIn("awaiting_receipt", pending[0]["reasons"])
        self.assertIn("missing_currency", pending[0]["reasons"])


class LedgerTest(unittest.TestCase):
    def test_old_page_unchanged_and_changes_flagged(self):
        corpus = load_corpus()
        book = book_at(11)
        report = reconcile(corpus, book)
        page = report["versions"][0]["pages"][0]
        self.assertEqual(page["locator"], "1923-09/T08")
        cited = {entry["sealed"]: entry for entry in page["entries"]}
        self.assertEqual(cited["甲七"]["cited"]["amount"]["value"], 1500)
        self.assertEqual(cited["甲七"]["current"]["amount"]["value"], 1000)
        self.assertEqual(cited["乙二"]["cited"]["status"], "proxy_unverified")
        self.assertEqual(cited["乙二"]["current"]["status"], "signed")
        changes = cited_claims_changed(report)
        self.assertEqual({row["sealed"] for row in changes}, {"甲七", "乙二"})
        self.assertTrue(all("M1" in row["cited_by"] for row in changes))

    def test_change_names_the_revision_that_declared_it(self):
        corpus = load_corpus()
        book = book_at(11)
        report = reconcile(corpus, book)
        entries = {e["sealed"]: e for e in report["versions"][0]["pages"][0]["entries"]}
        amount_change = entries["甲七"]["changed"][0]
        self.assertEqual(amount_change["declared_by"][0]["revision"], "X1")
        self.assertIn("R1", amount_change["declared_by"][0]["because"])
        status_change = entries["乙二"]["changed"][0]
        self.assertEqual(status_change["declared_by"][0]["revision"], "X1")
        self.assertIn("R2", status_change["declared_by"][0]["because"])

    def test_current_book_can_still_be_located_by_old_locator(self):
        corpus = load_corpus()
        book = book_at(11)
        report = reconcile(corpus, book)
        locators = {page["locator"] for version in report["versions"] for page in version["pages"]}
        self.assertIn("1923-09/T08", locators)


class RevisionTest(unittest.TestCase):
    def test_reopen_intent_line_does_not_move_other_household(self):
        corpus = load_corpus()
        ctx = EvidenceContext.build(corpus, 11)
        self.assertIn(("L1", "L1:2"), ctx.overrides)
        book = settle(ctx)
        # L1:2 的“内付五元”已重开，但 R1 的十元与 R2 的五元来源独立，照常入账。
        self.assertEqual(book["households"]["H1"]["received_totals"], {"银元": 1000})
        self.assertEqual(book["households"]["H2"]["received_totals"], {"银元": 500})

    def test_reopen_keeps_candidates(self):
        corpus = load_corpus()
        x1 = corpus.by_id("X1")
        reopen = x1.revision["reopen"][0]
        self.assertEqual(reopen["line"], "L1:2")
        self.assertEqual(reopen["to"], [])
        self.assertIn("乙二", reopen["note"])


class SharedReceiptTest(unittest.TestCase):
    def _two_batch_corpus(self, pointing: str, allocation=None) -> Corpus:
        records = [
            {"id": "L1", "kind": "银信", "text": "封标甲七。大银十元。", "arrival_order": 1,
             "party": "H1", "lines": [
                 {"id": "L1:1", "role": "face", "text": "封标甲七。大银十元。", "sealed_reference": "甲七",
                  "amounts": [{"value": 1000, "currency": "银元"}]}]},
            {"id": "L2", "kind": "银信", "text": "封标乙二。大银五元。", "arrival_order": 2,
             "party": "H2", "lines": [
                 {"id": "L2:1", "role": "face", "text": "封标乙二。大银五元。", "sealed_reference": "乙二",
                  "amounts": [{"value": 500, "currency": "银元"}]}]},
            {"id": "S1", "kind": "收据", "text": "收据", "arrival_order": 3,
             "party": "H1", "lines": [
                 {"id": "S1:1", "role": "signoff", "text": "收据",
                  "covers": ["甲七", "乙二"], "pointing": pointing,
                  "allocation": allocation,
                  "genuine_delivery": True,
                  "amounts": [{"value": 500, "currency": "银元"}, {"value": 500, "currency": "银元"}]}]},
        ]
        return from_records(records, households=[
            {"id": "H1", "name": "甲家", "aliases": []},
            {"id": "H2", "name": "乙家", "aliases": []},
        ])

    def test_pointing_alone_does_not_invent_amount(self):
        corpus = self._two_batch_corpus("余一封五／甲七收")
        book = settle(EvidenceContext.build(corpus))
        # 只提甲七、未写明两笔各归哪封：不替人把某一笔分给甲七。
        self.assertEqual(book["households"]["H1"]["received_totals"], {})
        self.assertEqual(book["households"]["H2"]["received_totals"], {})
        pending = [u for u in book["unallocated"] if u["record"] == "S1"]
        self.assertEqual(len(pending), 1)
        self.assertGreaterEqual(len(pending[0]["alternatives"]), 2)

    def test_allocation_uses_explicit_entries(self):
        corpus = self._two_batch_corpus("按封列分", {"甲七": {"value": 500, "currency": "银元"},
                                                     "乙二": {"value": 500, "currency": "银元"}})
        book = settle(EvidenceContext.build(corpus))
        self.assertEqual(book["households"]["H1"]["received_totals"], {"银元": 500})
        self.assertEqual(book["households"]["H2"]["received_totals"], {"银元": 500})

    def test_signature_order_allocates_distinct_amounts(self):
        records = [
            {"id": "L1", "kind": "银信", "text": "封标甲七。大银十五元。", "arrival_order": 1,
             "party": "H1", "lines": [
                 {"id": "L1:1", "role": "face", "text": "封标甲七。大银十五元。", "sealed_reference": "甲七",
                  "amounts": [{"value": 1500, "currency": "银元"}]}]},
            {"id": "L2", "kind": "银信", "text": "封标乙二。大银十五元。", "arrival_order": 2,
             "party": "H2", "lines": [
                 {"id": "L2:1", "role": "face", "text": "封标乙二。大银十五元。", "sealed_reference": "乙二",
                  "amounts": [{"value": 1500, "currency": "银元"}]}]},
            {"id": "S1", "kind": "收据", "text": "同一收据分两次签", "arrival_order": 3,
             "party": "H1", "lines": [
                 {"id": "S1:1", "role": "signoff", "text": "先签五元", "sealed_reference": "甲七",
                  "genuine_delivery": True,
                  "amounts": [{"value": 500, "currency": "银元"}]},
                 {"id": "S1:2", "role": "signoff", "text": "再签十元与五元",
                  "covers": ["甲七", "乙二"], "pointing": "按签收次序分配",
                  "genuine_delivery": True,
                  "amounts": [{"value": 1000, "currency": "银元"}, {"value": 500, "currency": "银元"}]}]},
        ]
        corpus = from_records(records, households=[
            {"id": "H1", "name": "甲家", "aliases": []},
            {"id": "H2", "name": "乙家", "aliases": []},
        ])
        book = settle(EvidenceContext.build(corpus))
        # 两种分法都在封面以内；甲七先签（次序 1），故先写的十元归甲七，五元归乙二。
        # 若反过来把五元给甲七，甲七只有十元，与“先签”次序不符。
        self.assertEqual(book["households"]["H1"]["received_totals"], {"银元": 1500})
        self.assertEqual(book["households"]["H2"]["received_totals"], {"银元": 500})

    def test_shared_transfer_allocated_by_sealed_marks(self):
        records = [
            {"id": "L1", "kind": "银信", "text": "封标甲七。大银十元。", "arrival_order": 1,
             "party": "H1", "lines": [
                 {"id": "L1:1", "role": "face", "text": "封标甲七。大银十元。", "sealed_reference": "甲七",
                  "amounts": [{"value": 1000, "currency": "银元"}]}]},
            {"id": "L2", "kind": "银信", "text": "封标乙二。大银五元。", "arrival_order": 2,
             "party": "H2", "lines": [
                 {"id": "L2:1", "role": "face", "text": "封标乙二。大银五元。", "sealed_reference": "乙二",
                  "amounts": [{"value": 500, "currency": "银元"}]}]},
            {"id": "F1", "kind": "交接", "text": "转交两封", "arrival_order": 3, "lines": [
                {"id": "F1:1", "role": "forward", "text": "代交两封",
                 "covers": ["甲七", "乙二"], "pointing": "按封列分",
                 "allocation": {"甲七": {"value": 300, "currency": "银元"},
                                "乙二": {"value": 200, "currency": "银元"}},
                 "amounts": [{"value": 300, "currency": "银元"}, {"value": 200, "currency": "银元"}]}]},
        ]
        corpus = from_records(records, households=[
            {"id": "H1", "name": "甲家"}, {"id": "H2", "name": "乙家"},
        ])
        book = settle(EvidenceContext.build(corpus))
        # 转交不等于家户收讫；两笔按封标分别挂到两户，也不合并。
        self.assertEqual(book["households"]["H1"]["received_totals"], {})
        self.assertEqual(book["households"]["H2"]["received_totals"], {})
        self.assertEqual([t["amount"]["value"] for t in book["households"]["H1"]["transfers"]], [300])
        self.assertEqual([t["amount"]["value"] for t in book["households"]["H2"]["transfers"]], [200])

    def test_unowned_covered_batch_blocks_allocation(self):
        records = [
            {"id": "L1", "kind": "银信", "text": "封标甲七。大银十元。", "arrival_order": 1,
             "party": "H1", "lines": [
                 {"id": "L1:1", "role": "face", "text": "封标甲七。大银十元。", "sealed_reference": "甲七",
                  "amounts": [{"value": 1000, "currency": "银元"}]}]},
            {"id": "L2", "kind": "银信", "text": "封标乙二。大银五元。", "arrival_order": 2,
             "lines": [
                 {"id": "L2:1", "role": "face", "text": "封标乙二。大银五元。", "sealed_reference": "乙二",
                  "amounts": [{"value": 500, "currency": "银元"}]}]},
            {"id": "S1", "kind": "收据", "text": "收据", "arrival_order": 3, "lines": [
                {"id": "S1:1", "role": "signoff", "text": "两封共收",
                 "covers": ["甲七", "乙二"], "pointing": "按封列分", "genuine_delivery": True,
                 "allocation": {"甲七": {"value": 500, "currency": "银元"},
                                "乙二": {"value": 500, "currency": "银元"}},
                 "amounts": [{"value": 500, "currency": "银元"}, {"value": 500, "currency": "银元"}]}]},
        ]
        corpus = from_records(records, households=[{"id": "H1", "name": "甲家"}])
        book = settle(EvidenceContext.build(corpus))
        # 乙二那封信没有家户证据，不能把收据全额记到甲家名下。
        self.assertEqual(book["households"]["H1"]["received_totals"], {})
        pending = [u for u in book["unallocated"] if u["record"] == "S1"]
        self.assertEqual(len(pending), 1)
        self.assertIn("missing_household", pending[0]["reasons"])
        self.assertIn("乙二", pending[0]["note"])

    def test_ambiguous_shared_receipt_not_allocated(self):
        corpus = self._two_batch_corpus("无指向")
        book = settle(EvidenceContext.build(corpus))
        self.assertEqual(book["households"]["H1"]["received_totals"], {})
        self.assertEqual(book["households"]["H2"]["received_totals"], {})
        pending = [u for u in book["unallocated"] if u["record"] == "S1"]
        self.assertEqual(len(pending), 1)
        self.assertGreaterEqual(len(pending[0]["alternatives"]), 2)

    def test_allocation_over_face_rejected(self):
        corpus = self._two_batch_corpus("按封列分", {"甲七": {"value": 500, "currency": "银元"},
                                                     "乙二": {"value": 1500, "currency": "银元"}})
        book = settle(EvidenceContext.build(corpus))
        # 甲七一项与封面相符，照常入账；乙二的超额项暂缓，不占用甲七已据之款。
        self.assertEqual(book["households"]["H1"]["received_totals"], {"银元": 500})
        self.assertEqual(book["households"]["H2"]["received_totals"], {})
        pending = [u for u in book["unallocated"] if u["record"] == "S1"]
        self.assertTrue(pending)
        self.assertIn("conflict", pending[0]["reasons"])


class ValidationTest(unittest.TestCase):
    def test_corpus_is_valid(self):
        findings = validate(load_corpus())
        self.assertFalse(has_errors(findings), [f.as_dict() for f in findings])

    def test_dangling_reference_detected(self):
        payload = load_corpus()
        broken = copy.deepcopy(payload)
        broken.records[2].references.append("NOPE")
        findings = validate(broken)
        self.assertTrue(has_errors(findings))

    def test_adopted_without_currency_warns(self):
        records = [{"id": "L1", "kind": "银信", "text": "x", "arrival_order": 1,
                    "lines": [{"id": "L1:1", "role": "face", "text": "五",
                               "amounts": [{"value": 500, "currency": None}]}]}]
        findings = validate(from_records(records))
        self.assertTrue(any(f.code == "adopted_without_currency" for f in findings))


class ReportTest(unittest.TestCase):
    def test_statement_links_back_to_line(self):
        corpus = load_corpus()
        book = book_at(11)
        stmt = household_statement(book, "H1")
        self.assertEqual(stmt["received_totals"], {"银元": 1000})
        self.assertEqual(stmt["lines"][0]["record"], "R1")
        self.assertEqual(stmt["lines"][0]["line"], "R1:1")
        self.assertTrue(stmt["lines"][0]["currency_basis"])


if __name__ == "__main__":
    unittest.main()
