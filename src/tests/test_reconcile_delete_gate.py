"""機械守線（Odoo 端）：reconcile 路徑 pre-merge grep gate（首版零自動刪除）。

對等 operator 端 `extra/paas-operator/tests/test_reconcile_delete_gate.py`。L4 reconciler 首版
是「自動發現孤兒、列清單給人看」的唯讀工具，**刪除權永遠留人手**。本 gate 對 Odoo reconcile
路徑檔強制「零刪除/零破壞操作」：任何 delete_* / 私有 _delete_* / Odoo unlink / helm uninstall /
scale-to-zero / rollback / 直接 subprocess shell out 一旦出現即 CI 紅，把 dry-run-first 從人工
自律變成 fail-closed 機械守線（設計 doc §安全模型 / §0.1 / §機械守線）。

精準度：以 `tokenize` 先抹「註解 + 所有字串字面值」，只對可執行 code 套 pattern → reconciler.py
合法的免責 docstring、dry-run reason 字串、`orphan_candidates` 命名零誤判（同 operator 端紀律）。

**pattern 對齊「Odoo client 的實際破壞面」，非盲抄 operator 清單**（對抗式 self-test 抓到的教訓）：
Odoo reconcile 路徑呼叫的是 client `paas_operator.py`，其破壞方法名（`uninstall_release` /
`scale_release` / `rollback_release` / `delete_tunnel` / `configure_tunnel`）與 operator 的 service
方法名（`uninstall` / `scale_deployment` / `reconcile_tunnel_dns`…）**不同**。直接套 operator 的
`\\buninstall\\b` / `\\brollback\\b` 會因尾界（`_` 是 word char）**漏抓** `uninstall_release` /
`rollback_release`。故此清單獨立對齊 Odoo client 真實 def（見 `paas_operator.py`）；兩端各守自家
repo 破壞面，共用泛型（`delete_` / `unlink` / `subprocess`），specifics 各異。新增 client 破壞方法
時須同步補 pattern（self-test 會逼出漏抓）。

Odoo 共用 client 有為其他 feature 而存在的合法 delete_*（不可整檔納管）→ gate 只守 reconcile
**專屬**檔（reconciler.py + 未來 cron / 報表）；`reconcile_orphans` 的唯讀性由「它是 HTTP GET
薄包裝」+ code review 守，非檔案 gate。
"""
import io
import re
import tokenize
import unittest
from pathlib import Path

ADDON_SRC = Path(__file__).resolve().parent.parent  # .../src

# reconcile 路徑禁用的「可執行 code」token（字串/註解已被 _strip 抹除）。
# 對齊 Odoo client `paas_operator.py` 的實際破壞方法 def（非盲抄 operator service 名）。
FORBIDDEN_PATTERNS = [
    r"(?<![A-Za-z0-9])_?delete_",      # delete_tunnel / 私有 _delete_*（\b 漏 leading-underscore）
    r"\.delete\s*\(",                   # client.delete( / recordset.delete(
    r"\buninstall",                     # uninstall_release（**不收尾 \b**：_ 是 word char，\b 會漏）
    r"\bunlink\b",                      # Odoo record.unlink() / Path.unlink()
    r"\bscale_release\b",               # client scale_release（scale-to-zero teardown；唯讀路徑不該 scale）
    r"\brollback",                      # rollback_release（**不收尾 \b**，理由同 uninstall）
    r"\bconfigure_tunnel",              # configure_tunnel（變更 ingress/route；唯讀路徑不該寫 tunnel config）
    r"\bsubprocess\b",                  # reconcile 路徑不該直接 shell out
]

# Odoo reconcile **專屬**路徑檔（未建者 gate 對其 vacuous；一落地即自動納管）。
# controller 純讀（list / reverify）、無自家 row unlink → 走全量 pattern（連 unlink 都禁，
# 未來若加「刪除此孤兒」endpoint 會被 gate 擋下，逼維持發現↔刪除分離）。
RECONCILE_PATH_FILES = [
    ADDON_SRC / "services" / "reconciler.py",
    ADDON_SRC / "controllers" / "reconcile_report.py",
]

# report 路徑（``models/reconcile_report.py``：model + cron + snapshot 持久化）：snapshot-replace
# 需 ``unlink`` **自家報表列**（Odoo bookkeeping，非 CF/k8s 資源刪除）→ 僅豁免 ``\bunlink\b`` 一條，
# 其餘 operator client 資源破壞向量仍全禁。豁免精確鎖死（見 ``TestReconcileReportPathDeleteGate``）。
_UNLINK_PATTERN = r"\bunlink\b"
REPORT_FORBIDDEN_PATTERNS = [p for p in FORBIDDEN_PATTERNS if p != _UNLINK_PATTERN]
REPORT_PATH_FILES = [
    ADDON_SRC / "models" / "reconcile_report.py",
]


def _strip_comments_and_strings(source):
    """用 tokenize 把「註解 + 所有字串字面值」抹成等長空白，只留可執行 code（同 operator 端）。"""
    rows = [list(line) for line in source.split("\n")]

    def blank(srow, scol, erow, ecol):
        for r in range(srow, erow + 1):  # tokenize row 1-based
            if r - 1 >= len(rows):
                break
            row = rows[r - 1]
            c0 = scol if r == srow else 0
            c1 = ecol if r == erow else len(row)
            for c in range(c0, min(c1, len(row))):
                row[c] = " "

    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                blank(tok.start[0], tok.start[1], tok.end[0], tok.end[1])
    except (tokenize.TokenError, IndentationError):
        return source.split("\n")  # 保守退回（寧多抓不漏抓）
    return ["".join(row) for row in rows]


def _scan_forbidden(source, patterns=FORBIDDEN_PATTERNS):
    hits = []
    for lineno, code in enumerate(_strip_comments_and_strings(source), start=1):
        for pattern in patterns:
            if re.search(pattern, code):
                hits.append((lineno, pattern, code.strip()))
    return hits


class TestReconcileDeleteGate(unittest.TestCase):
    """Odoo reconcile 路徑零刪除/破壞 token（首版零自動刪除）。"""

    def test_forbidden_detector_catches_all_deletion_vectors(self):
        """detector 自證非虛設：Odoo client 各破壞方法（真實 def 名）必須全數命中。

        特別釘住 `uninstall_release` / `rollback_release` —— operator 的 `\\buninstall\\b` /
        `\\brollback\\b` 因 `_` 是 word char 會漏抓這兩個（盲抄 operator 清單的真缺口）。
        """
        code = "\n".join([
            "client.delete_tunnel(tid)",                       # 1: delete_
            "client._delete_dns_record(rid)",                  # 2: 私有 _delete_
            "await client.delete(url)",                        # 3: .delete(
            "client.uninstall_release(ns, rel)",               # 4: uninstall_release（尾界陷阱）
            "record.unlink()",                                 # 5: unlink
            "client.scale_release(ns, rel, 0)",                # 6: scale-to-zero
            "client.rollback_release(ns, rel, rev)",           # 7: rollback_release（尾界陷阱）
            "client.configure_tunnel(tid, host, svc)",         # 8: 寫 tunnel config
            "subprocess.run(['kubectl', 'delete', 'ns'])",     # 9: 直接 shell out
        ])
        matched = {ln for ln, _, _ in _scan_forbidden(code)}
        self.assertEqual(matched, set(range(1, 10)),
                         f"detector 漏抓：{sorted(set(range(1, 10)) - matched)}")

    def test_forbidden_detector_ignores_prose_and_readonly_code(self):
        """docstring / 註解 / reason·log 字串 / 唯讀 token 不誤判（避免逼作者改寫文案繞 gate）。"""
        code = "\n".join([
            '"""唯讀對帳：絕不 unlink、絕不 uninstall、絕不呼叫任何 delete_ 方法。"""',
            "# 人工 runbook：列出後由人 helm uninstall / kubectl delete，本程式不執行",
            'reason = "candidate for manual delete_tunnel cleanup"',
            'logger.info("dry-run: would uninstall %s manually", name)',
            "orphan_candidates = right_set - left_set",
            "ls.tunnel_ids |= _clean(sa.mapped('tunnel_id'))",
            "candidates_for_deletion = []",
        ])
        self.assertEqual(_scan_forbidden(code), [],
                         "誤判唯讀/prose 行")

    def test_gate_is_armed_with_reconcile_paths(self):
        """gate 必須有設定要守的 reconcile 路徑清單（非空），否則永遠 vacuous。"""
        self.assertTrue(RECONCILE_PATH_FILES, "reconcile 路徑清單為空 → gate 沒在守任何東西")

    def test_reconcile_path_files_have_no_delete(self):
        """reconcile 路徑檔（存在者）零刪除/破壞 token（首版零自動刪除）。"""
        offenders = {}
        for path in RECONCILE_PATH_FILES:
            if not path.exists():
                continue
            hits = _scan_forbidden(path.read_text(encoding="utf-8"))
            if hits:
                offenders[path.name] = hits
        self.assertFalse(offenders, f"reconcile 路徑出現刪除/破壞 token：{offenders}")


class TestReconcileReportPathDeleteGate(unittest.TestCase):
    """report 路徑（``models/reconcile_report.py``：model + cron + snapshot 持久化）的 delete-gate。

    與 reconciler.py（純讀、連自家 unlink 都禁）不同：報表 model 做 snapshot-replace 需 ``unlink``
    **自家報表列**（Odoo bookkeeping，非 CF/k8s 資源）。故 report-path **僅豁免 ``\\bunlink\\b``
    一條**，所有經 operator client 真去刪/改遠端資源的破壞向量（``delete_*`` / ``uninstall_release``
    / ``scale_release`` / ``rollback_release`` / ``configure_tunnel`` / ``subprocess`` / ``.delete(``）
    **仍全禁**。豁免精確鎖死，不得靜默放寬其他破壞 pattern（否則報表路徑變後門）。
    """

    def test_report_path_is_armed(self):
        """report 路徑清單非空（否則 gate vacuous）。"""
        self.assertTrue(REPORT_PATH_FILES, "report 路徑清單為空 → gate 沒在守任何東西")

    def test_report_exemption_is_exactly_unlink(self):
        """豁免鎖死：report-path 相對全量 pattern **只**少 ``\\bunlink\\b`` 一條，不多不少。"""
        self.assertEqual(
            set(FORBIDDEN_PATTERNS) - set(REPORT_FORBIDDEN_PATTERNS),
            {r"\bunlink\b"},
            "report-path 只應豁免 unlink；其餘破壞 pattern 必須全保留（豁免變寬 = 後門）",
        )

    def test_report_gate_still_catches_resource_destruction(self):
        """detector 自證非虛設：豁免 unlink 後，report-path 仍抓所有 operator client 資源破壞向量。"""
        code = "\n".join([
            "client.delete_tunnel(tid)",                  # 1
            "client._delete_dns_record(rid)",             # 2
            "client.uninstall_release(ns, rel)",          # 3
            "client.scale_release(ns, rel, 0)",           # 4
            "client.rollback_release(ns, rel, rev)",      # 5
            "client.configure_tunnel(tid, h, s)",         # 6
            "subprocess.run(['kubectl', 'delete'])",      # 7
            "await client.delete(url)",                   # 8
        ])
        matched = {ln for ln, _, _ in _scan_forbidden(code, REPORT_FORBIDDEN_PATTERNS)}
        self.assertEqual(matched, set(range(1, 9)),
                         f"report-path 漏抓資源破壞向量：{sorted(set(range(1, 9)) - matched)}")

    def test_report_gate_allows_own_row_unlink(self):
        """自家報表列 unlink（snapshot-replace bookkeeping）放行——這正是豁免的目的。"""
        code = "Report.search([]).unlink()"
        self.assertEqual(_scan_forbidden(code, REPORT_FORBIDDEN_PATTERNS), [],
                         "自家 row unlink 不該被 report-path gate 擋")

    def test_report_path_files_have_no_resource_destruction(self):
        """report 路徑檔（存在者）零 **資源破壞** token（自家 row unlink 例外）。"""
        offenders = {}
        for path in REPORT_PATH_FILES:
            if not path.exists():
                continue
            hits = _scan_forbidden(path.read_text(encoding="utf-8"), REPORT_FORBIDDEN_PATTERNS)
            if hits:
                offenders[path.name] = hits
        self.assertFalse(offenders, f"report 路徑出現資源破壞 token：{offenders}")
