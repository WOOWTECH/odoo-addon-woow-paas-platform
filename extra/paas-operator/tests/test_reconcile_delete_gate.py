"""TS-1 機械守線：reconcile 路徑 pre-merge grep gate（首版零自動刪除）。

L4 reconciler 首版是「自動發現孤兒、列清單給人看」的唯讀工具，**刪除權永遠留在人手**。
本 gate 對 reconcile 路徑檔強制「零刪除/零破壞操作」——任何 delete_* / 私有 _delete_* /
HTTP DELETE / helm uninstall / Odoo unlink / 以及「無 delete 字眼但會抹除資源」的破壞向量
（PUT-erase ingress、scale-to-zero、helm rollback、宣告式 reconcile_tunnel_dns([])）或直接
shell out（subprocess）一旦出現在 reconcile 路徑即 CI 紅，把「dry-run-first」從人工自律變成
fail-closed 的機械守線（設計 doc §安全模型 / §0.1 / §機械守線）。

**精準度（呼應對抗式審查）**：以 ``tokenize`` 先抹掉「註解 + 所有字串字面值」，只對真正的
可執行 code 套 pattern。故 reconcile.py 合法的免責 docstring（「絕不 unlink/uninstall/
delete_」）、人工 runbook 註解、dry-run log / reason 字串都**不會**被誤判，作者無須改寫文案
繞過 gate；同理 ``deleted_at`` 軟刪過濾、``orphan_candidates`` 命名也零誤判。

**本質是 fail-closed tripwire，非完備證明**：只認 operator 既有刪除/破壞動詞 + subprocess。
若 reconcile 將來引入新破壞動詞（remove/destroy/purge…）或字串拼接式刪除，須同步補 pattern。
偵測邏輯非虛設由 detector self-test 擔保。operator 端 reconcile 路徑檔（``src/api/reconcile.py``
等）於 TS-2 / Phase A.1 才建立；在那之前 gate 對「實檔」vacuous，檔案一落地即自動納管。Odoo
端（``src/services/reconciler.py`` + cron / 報表）的對等 gate 由主 repo ``src/tests/`` 在
TS-3 / Phase D 檔案落地時補。
"""
import io
import re
import tokenize
from pathlib import Path

OPERATOR_ROOT = Path(__file__).resolve().parent.parent
OPERATOR_SRC = OPERATOR_ROOT / "src"

# reconcile 路徑禁用的「可執行 code」token（字串/註解已被 _strip 抹除，不在掃描範圍）。
# 對齊 operator 既有的真實刪除/破壞向量（src/services/cloudflare.py, helm.py）：
FORBIDDEN_PATTERNS = [
    r"(?<![A-Za-z0-9])_?delete_",      # delete_tunnel / delete_namespace / 私有 _delete_access_app（\b 會漏 leading-underscore）
    r"\.delete\s*\(",                   # httpx / client.delete( ... ) HTTP DELETE
    r"\buninstall\b",                   # helm uninstall
    r"\bunlink\b",                      # Odoo record.unlink() / Path.unlink()
    r"\bupdate_tunnel_config\b",        # PUT 整份 ingress（少給一條 rule = 等效刪該 route）
    r"\bconfigure_tunnel_ingress\b",    # PUT 空 rules = 抹掉某 tunnel 全部 route
    r"\breconcile_tunnel_dns\b",        # 宣告式：desired=[] → 內部刪光該 tunnel 全部 DNS
    r"\bscale_deployment\b",            # scale-to-zero = 軟 teardown（唯讀路徑不該 scale 任何值）
    r"\bscale_statefulset\b",
    r"\brollback\b",                    # helm rollback 會移除自該 rev 後新增的資源
    r"\bsubprocess\b",                  # reconcile 路徑不該直接 shell out（須走 service 層唯讀方法）
]

# operator 端 reconcile 路徑檔（未建者 gate 對其 vacuous；一落地即自動納管）。
RECONCILE_PATH_FILES = [
    OPERATOR_SRC / "api" / "reconcile.py",
    OPERATOR_SRC / "services" / "reconciler.py",
]


def _strip_comments_and_strings(source):
    """用 tokenize 把「註解 + 所有字串字面值」抹成等長空白（保留欄位位置），只留可執行 code。

    刻意連一般字串都抹（非只 docstring）：reconcile.py 的 dry-run log / reason 字串可能合法地
    含 'delete'/'uninstall' 等字眼，抹掉才不誤判；真正的刪除是 **method 呼叫**（code，非字串）。
    與 test_async_harness_guard.py 用 AST 精準偵測同一紀律（不靠裸 grep 誤抓字串字面值）。
    """
    rows = [list(line) for line in source.split("\n")]

    def blank(srow, scol, erow, ecol):
        for r in range(srow, erow + 1):  # tokenize row 是 1-based
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
        # tokenize 失敗（不完整 source）→ 保守退回原始（寧可多抓不漏抓刪除）
        return source.split("\n")
    return ["".join(row) for row in rows]


def _scan_forbidden(source):
    """回傳 [(lineno, pattern, code)]，命中任一禁用刪除/破壞 token 的「可執行 code」行。"""
    hits = []
    for lineno, code in enumerate(_strip_comments_and_strings(source), start=1):
        for pattern in FORBIDDEN_PATTERNS:
            if re.search(pattern, code):
                hits.append((lineno, pattern, code.strip()))
    return hits


def test_forbidden_detector_catches_all_deletion_vectors():
    """detector 自證：operator 各類刪除/破壞向量（含私有 _delete_）必須全數命中（防 gate 虛設）。

    這是 gate 的非-vacuity 證明——reconcile 實檔尚未建立時，仍確保偵測邏輯真的會咬人。
    特別釘住對抗式審查抓到的 ``\\bdelete_`` 漏 leading-underscore 私有方法（line 2）。
    """
    code = "\n".join([
        "result = await cf.delete_tunnel(tid)",            # 1: delete_
        "await cf._delete_access_app(app_id)",             # 2: 私有 _delete_（\b 會漏，本 gate 必抓）
        "await client.delete(url)",                        # 3: .delete(
        "helm_service.uninstall(ns, release)",             # 4: uninstall
        "record.unlink()",                                 # 5: unlink
        "cf.update_tunnel_config(tid, pruned_rules)",      # 6: PUT-erase ingress
        "cf.configure_tunnel_ingress(tid, [])",            # 7: 抹掉 tunnel 全部 route
        "await cf.reconcile_tunnel_dns(tid, [])",          # 8: 宣告式刪光 DNS
        "helm_service.scale_deployment(ns, dep, 0)",       # 9: scale-to-zero
        "helm_service.scale_statefulset(ns, sts, 0)",      # 10: scale-to-zero
        "helm_service.rollback(ns, release, rev)",         # 11: rollback
        "subprocess.run(['kubectl', 'delete', 'ns', n])",  # 12: 直接 shell out
    ])
    matched = {lineno for lineno, _, _ in _scan_forbidden(code)}
    expected = set(range(1, 13))
    assert matched == expected, f"detector 漏抓：{sorted(expected - matched)}（命中={sorted(matched)}）"


def test_forbidden_detector_ignores_prose_and_readonly_code():
    """docstring / 註解 / log·reason 字串 / 唯讀 token 一律不誤判（呼應對抗式審查 false-positive）。

    reconcile.py 合法地會有：描述 no-delete 不變式的 docstring、人工 runbook 註解、dry-run
    log 與 reason 字串、deleted_at 軟刪過濾、orphan_candidates 命名、list_all_* 唯讀呼叫。
    這些都**不該**讓 gate 紅，否則逼作者改寫文案繞過 grep、侵蝕 gate 可信度。
    """
    code = "\n".join([
        '"""唯讀對帳：絕不 unlink、絕不 uninstall、絕不呼叫任何 delete_ 方法。"""',  # docstring
        "# 人工 runbook：列出後由人 helm uninstall / kubectl delete，本程式不執行",   # 註解
        'reason = "candidate for manual delete_tunnel cleanup"',                      # reason 字串
        'logger.info("reconcile dry-run: would uninstall %s manually", name)',        # log 字串
        'if rec.get("deleted_at") is not None:',                                      # 軟刪過濾
        "orphan_candidates = left_set - right_set",                                   # design 命名
        "tunnels = await cf.list_all_tunnels()",                                      # 唯讀呼叫
        "candidates_for_deletion = []",                                               # 合法 var 名
    ])
    hits = _scan_forbidden(code)
    assert not hits, f"誤判唯讀/prose 行：{hits}"


def test_gate_is_armed_with_reconcile_paths():
    """gate 必須有設定要守的 reconcile 路徑清單（非空），否則永遠 vacuous = 形同關閉。"""
    assert RECONCILE_PATH_FILES, "reconcile 路徑清單為空 → gate 沒在守任何東西"


def test_reconcile_path_files_have_no_delete():
    """reconcile 路徑檔（存在者）必須零刪除/破壞 token（首版零自動刪除）。

    檔案未建立時對其 vacuous（forward guard）；non-vacuity 由 detector self-test 擔保。
    """
    offenders = {}
    for path in RECONCILE_PATH_FILES:
        if not path.exists():
            continue
        hits = _scan_forbidden(path.read_text(encoding="utf-8"))
        if hits:
            offenders[str(path.relative_to(OPERATOR_ROOT))] = hits
    assert not offenders, (
        "reconcile 路徑出現刪除/破壞 token（首版必須零自動刪除、唯讀產 dry-run 清單）：\n"
        + "\n".join(
            f"  {f}:\n" + "\n".join(f"    L{ln} [{pat}] {src}" for ln, pat, src in hits)
            for f, hits in offenders.items()
        )
    )
