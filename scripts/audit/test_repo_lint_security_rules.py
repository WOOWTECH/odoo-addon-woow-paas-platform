#!/usr/bin/env python3
"""#659 三條 ir.rule 守線的**注入測試**（紅燈優先，不是「跑一遍看乾不乾淨」）。

守的三條（實作見 `repo-lint.py` 檔頭第 22／23 條）：

  ① `check_security_rule_noupdate`            ir.rule 不得**出生**在 noupdate="1" 區塊
  ② `check_retired_rule_xmlid`                已退役的 xmlid 不得**復活**
  ③ `check_workspace_access_rule_directions`  workspace_access 的寫入方向不得**留空**

  ④ `check_workspace_rule_directions`         workspace **本體**的寫入方向不得**留空**

⚠ ② 原本寫「#556 退役的 xmlid」——#658 又退役了 `workspace_rule_member_access` /
`workspace_rule_create`（migration `18.0.1.0.129`），權威清單是 `RETIRED_RULE_XMLIDS`
本身，不是這段散文。

④ 是 #658 新增的。它與 ③ 共用實作（`_rule_direction_violations`），所以**判準那一半
由 ③ 的測試覆蓋**；④ 自己真正沒被驗的是**它的錨點**——`WORKSPACE_MODEL_REF` 到底指向
哪個 model。錨點指錯 ⇒ selector 永不命中 ⇒ **它會永遠是綠的，而名字照樣印在通過清單
裡**（本 repo 記錄有案的 no-op gate 家族）。③ 的測試**結構上不可能**涵蓋這一格：它們
注入的是 `workspace_access` 的 record，④ 指錯 model 時那些測試一條都不會變色。
所以本檔對 ④ 的測試全部錨在「它看的是 workspace 本體」這個唯一的區別點
——見 `TestWorkspaceModelDirectionAnchor`。

🔴 #675 覆核補的兩格（原本那一支最小注入測試對它們**零鑑別力**）：

  * **F1 錨點被複製貼上**：原本那支注入的是「翻掉 workspace 本體 manage rule 的
    perm_write」，而 `WORKSPACE_MODEL_REF` 一旦被貼成 `WORKSPACE_ACCESS_MODEL_REF`
    的值，④ 看的就是 ③ 那組真的存在、真的有 rule 的 record ⇒ 那個注入對它毫無影響、
    全綠。覆核者實測到底：再把 workspace 本體四條 rule 全刪，`--all` 仍 EXIT=0。
    ⇒ `TestWorkspaceModelDirectionAnchor` 現在同時守「打錯字」「複製貼上」「錨點
    對不到任何 rule」三種形態，外加一支**存在性證明**（兩個錨點的 rule 集合非空
    且不相交＝兩層判準真的是兩層）與一支注入手法自身的絆線。
  * **F2 `RETIRED_RULE_XMLIDS` 沒有成員 floor**：本檔②的每一支都是拿那份清單自己
    當迴圈來源，少一個名字只會少跑一輪 ⇒ 覆核者實測拿掉兩個條目，repo-lint EXIT=0、
    `Ran 52 tests OK`，零訊號。floor 現在是獨立列舉的 `RETIRED_RULE_XMLIDS_FLOOR`
    （見它的註解），並折進 `test_every_floor_name_is_individually_blocked_by_the_gate`
    的迴圈。

## 為什麼第一支測試是注入，不是「clean tree 全綠」

「跑一遍看乾淨」對「selector 從未命中」的 no-op gate **零鑑別力**——整條沒在跑的那個
gate，名字照樣印在通過清單裡，內部判準逐行讀也都對。本 repo 記錄有案（memory:
`feedback_negative_verification_needs_positive_control`）：探針壞在**取樣層**時，判準層
再對都沒用。三條守線裡有兩條是**負向**判準（「不得出現 X」「不得住在 Y」），負向判準
在空集合上恆真 ⇒ 探針壞掉與乾淨的輸出完全相同。

所以本檔的每一支敏感度測試都是「注入 → 必須紅**且點名是哪一個** → 還原 → 必須綠」，
而且注入工具自帶絆線（`InjectionFailed`）：錨點沒命中時 **raise**，不會靜默回傳一份
未突變的原始碼讓測試假綠。

## 🔴 `TestNoupdateNegativeSideIsNotAnEscape`＝一條實際存在過的靜默逃逸路徑的回歸測試

原始實作把 `noupdate` 的值判定寫成 `misc.py :: str2bool` 的白名單語意，於是
`<data noupdate="no">` 底下的 ir.rule **在 Odoo 是 sticky noupdate、gate 卻放行**
（獨立驗收 #659 F1 端到端坐實 `repo-lint EXIT=0`）。根因是 Odoo 有**兩個**同名
`str2bool`：`convert.py` 自己定義了一個**純黑名單**版（`value.lower() not in
('0','false','off')`，永不 raise），XML 的 `noupdate` 走的是那一份。當時 38 支測試
**一支都沒有測否定側**——肯定側（`1/true/yes/on`）全對，所以整組看起來完備。
⇒ 教訓不是「少測一個值」，是**布林判準的兩側要分別列舉**；只驗肯定側的表對否定側
零鑑別力。

## 敏感度與特異性成對

只驗「改壞→紅」的測試集裡，一條恆紅的 gate 看起來完美——直到它擋住正當工作然後被人
整條拿掉。所以每組都配特異性：合法形態（`<data noupdate="0">` 的 rule、內層 noupdate="0"
覆寫外層、**只在註解裡提到**退役名字、global rule、`eval="ref('…')"` 的 model 綁定）
一律不得被誤擋。其中「只在註解裡提到退役名字」是現況就存在的形態（`ir_rules.xml` 的
註解、migration 的 `_RETIRED` tuple、landing doc 都寫著那兩個名字），字串判準會把史料
當違規。

## 🔴 這一組的核心：`test_group_system_only_rule_does_not_cover_a_direction`

③ 的判準刻意**不是**「這個 model 有沒有任一條 ir.rule」。Odoo `ir.rule._get_rules` 的
SQL 只收 `r.global OR r.id IN (使用者所屬 group 的 rule)`，所以只掛 `base.group_system`
的 `*_rule_admin_full` 對純 `base.group_user` 租戶帳號完全不構成限制；而本 repo 每個
model 都配一條 admin_full ⇒ 寬判準會把「洞開著」讀成「有守」，補一條 admin_full 就能
讓 gate 轉綠卻沒修任何東西（memory:
`reference_odoo_group_system_only_rule_does_not_constrain_group_user`，#524 在 prod DB
配陽性對照實測過）。那一支測試就是把「寬判準會綠、嚴判準會紅」釘死成契約。

## 本檔自己的鑑別力（2026-08-10 / 08-11 實測，附可重跑的配方）

「全綠」對一份**沒有鑑別力**的測試檔完全是同一個輸出。所以做過反向實驗：把
`repo-lint.py` 逐一突變成「錯的設計」再跑本檔，八個突變**全部**被抓到（每次都還原）：

  | 突變（把 gate 改成錯的設計）                     | 該紅的測試 |
  | ------------------------------------------------ | ---------- |
  | 判準放寬成「任一條 ir.rule 即算有守」            | `test_group_system_only_rule_does_not_cover_a_direction` |
  | `perm_*` 欄位缺省改判 False（違背 Odoo default） | `test_an_omitted_perm_field_counts_as_true` |
  | `_ir_rule_premise` 改成 `return []`（fail-open） | `TestFailLoud` 全 7 支 |
  | xmlid 不做 module 前綴正規化                     | `test_module_prefixed_form_is_also_red` |
  | 拿掉 allowlist 的反向對帳                        | `test_an_allowlist_entry_with_no_matching_record_is_red` |
  | `noupdate` 判定退回 `misc.py` 白名單（＝F1 的洞） | `TestNoupdateNegativeSideIsNotAnEscape` 全 7 支 |
  | 訊息把 ACL 值寫死回 `1,1,1,1`                    | `TestViolationMessageDoesNotAssertStaleFacts` 全 3 支 |
  | 訊息把 `noupdate` 值寫死回 `1`                   | `test_every_sticky_spelling_is_red_and_prints_the_actual_value` |

要重跑就照這張表逐條改 `repo-lint.py` 再跑本檔（每條改完務必還原）。這八個突變不是
隨便挑的——它們一一對應三條守線的**設計決定**，也就是下一個人最可能「順手簡化」掉
的那幾處。⚠ 重跑時**注入前先確認錨點命中恰好一次**：08-11 那輪有一條錨點縮排寫錯 ⇒
命中 0 次，`str.replace` 原樣回傳，若沒有那道 assert 就會把「注入根本沒發生」讀成
「gate 有問題／沒問題」。

## 🔴 跑上面那份配方之前先讀這段：它對 `.pyc` stale cache **毫無防備，失敗方向是假綠**

上面那張突變表的形態是「**就地改 `repo-lint.py` 的原始碼 → 跑本檔 → 還原**」。
本檔用 `importlib` 從**檔案路徑**載 `repo-lint.py`，走 `SourceFileLoader`，而它的
bytecode cache 驗證判準只有 `(mtime 秒, size)` 兩個值。就地突變非常容易同時避開兩者：

  - **替換值長度相同** ⇒ size 不變。`"…0.129（#658）"` → `"…0.999（#658）"` 正是這種
    （改版號、翻一個字元、`True`↔`Fals`… 都會踩到）。
  - **突變與 `cp` 還原落在同一秒** ⇒ mtime 的秒數不變。

兩個一起成立，Python 就判定 `__pycache__/repo-lint.cpython-3xx.pyc` 仍然有效，載進來的
是**舊的** bytecode——你改的東西根本沒生效。

🔴 **後果的形狀比機制重要：失敗方向是「回報綠」。**
2026-08-11（#675 追加 A）實測：磁碟上明明是 `127`，不清 cache 讀到的是 `999`；那一輪
RED 驗證**兩個方向都錯**——先把「其實會紅」的兩支讀成綠（差一點據此下結論「新寫的測試
沒有鑑別力」，**與事實完全相反**），還原之後又把乾淨的樹讀成紅。
⇒ 拿到「突變了卻沒紅」時**先懷疑探針，不要先懷疑測試**。

**可照抄的配方（每一次突變迴圈都照這樣做）：**

    # 1) 備份
    cp scripts/audit/repo-lint.py /tmp/rl.bak
    # 2) 突變（務必自檢錨點「命中恰好一次」，沒命中就中止——見 InjectionFailed 的理由）
    python3 - <<'"'"'EOF'"'"'
    ...
    EOF
    # 3) 清 cache 並用 -B 跑
    rm -rf scripts/audit/__pycache__ && python3 -B -m unittest scripts.audit.test_repo_lint_security_rules
    # 4) 還原
    cp /tmp/rl.bak scripts/audit/repo-lint.py
    # 5) 再清一次 cache 跑，確認回綠（少了這步，還原有沒有生效也不知道）
    rm -rf scripts/audit/__pycache__ && python3 -B -m unittest scripts.audit.test_repo_lint_security_rules

**兩道獨立的防線，兩道都要：**

  1. `rm -rf scripts/audit/__pycache__` **＋** `python3 -B`。`-B` 只擋**寫入**，既有的
     `.pyc` 照樣會被讀 ⇒ 少了 `rm -rf` 等於沒防。
  2. **選突變值時避開「長度相同」**（例：把 `18.0.1.0.129` 改成 `18.0.1.0.9999` 而不是
     `18.0.1.0.999`）。這是第二道保險，**不能取代第一道**——mtime 同秒仍可單獨咬人。

一句話判準：**改完原始碼之後，先驗「跑起來讀到的值真的變了」再看紅綠。**

⚠ 這個陷阱**只咬「改 `repo-lint.py` 自己」那種突變**。本檔測試內部的注入都在
`tempfile` 假 root 上做（改的是 XML 資料檔，不是被 import 的 Python 模組），不受影響；
正常跑 `python3 scripts/audit/repo-lint.py` 是 `__main__`、也不吃 cache。所以它不是
產品問題，是**這份配方**的問題。

## 不動 repo 一個字

所有注入都在 `tempfile.mkdtemp()` 造的假 repo root 上做（`<tmp>/src/security/ir_rules.xml`），
被測 check 吃 root 參數，所以真檔案一個字都不會被改到（也就不存在「注入沒還原就 commit」
這個風險）。基準 fixture 一律**從真檔案複製**而不是手抄：手抄的 fixture 會與真實佈局脫鉤，
真檔案改形狀時它照樣綠。

跑法：
    python3 scripts/audit/test_repo_lint_security_rules.py
    # 或 python3 -m pytest scripts/audit/test_repo_lint_security_rules.py

契約（.gitea/workflows/repo-lint.yml 的「scripts/ 自我測試」步驟）：不得有外部依賴
——不碰 ~/.claude/、kubeconfig、helm、網路、Odoo、git 執行檔，也不依賴 cwd 與
`CLAUDE_PROJECT_DIR`（root 一律顯式傳入）。
"""
import importlib.util
import os
import pathlib
import re
import shutil
import tempfile
import unittest

_GATE_PATH = pathlib.Path(__file__).with_name("repo-lint.py")
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_REAL_RULES_REL = "src/security/ir_rules.xml"
_REAL_RULES = _REPO_ROOT / _REAL_RULES_REL
_MIGRATIONS_ROOT = _REPO_ROOT / "src/migrations"

# `RETIRED_RULE_XMLIDS` 的值形如 `"18.0.1.0.122（#556）"`——版號在前，出處 issue 在後。
_VERSION_IN_PROVENANCE = re.compile(r"^\s*([0-9][0-9.]*)")


def retirement_migration(provenance):
    """從 `RETIRED_RULE_XMLIDS` 的**值**推導出「退役它的那支 migration」的路徑。

    🔴 刻意不寫死任何一支 migration 的版號（`18.0.1.0.122` 那份寫法是 #658 撞到的
    F1）：寫死的版號把「當時唯一的退役事件」凍成契約，第二次退役（#658 的
    `18.0.1.0.129`）發生的當天那條測試就失去靶子，而它的失敗訊息會說「名字不在
    migration 裡」——把讀的人導向完全錯誤的方向（以為退役機制壞了）。

    版號從值裡解析而不是另外維護一張表：`RETIRED_RULE_XMLIDS` 已經是那份對應關係的
    單一真相源，再開一份就會漂。解析不出來回 None，由呼叫端當**失敗**處理
    （不是靜默略過——那會讓這條測試在值的格式改掉的當天無聲失去鑑別力）。
    """
    hit = _VERSION_IN_PROVENANCE.match(provenance or "")
    if not hit:
        return None
    return _MIGRATIONS_ROOT / hit.group(1) / "post-migrate.py"


def _load_gate():
    """把 repo-lint.py 載成模組（檔名有連字號，只能走 importlib）。"""
    spec = importlib.util.spec_from_file_location("repo_lint_659", _GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rl = _load_gate()

_WA_REF = rl.WORKSPACE_ACCESS_MODEL_REF
_TENANT = rl.TENANT_GROUP_XMLID
_ADMIN_GROUP = "base.group_system"
_PERMS = ("perm_read", "perm_write", "perm_create", "perm_unlink")
_DIRECTIONS = rl.IR_RULE_WRITE_DIRECTIONS

# 🔴 ② 的**成員 floor**：這幾個名字**逐一列舉**，不是 `assertTrue(清單非空)`（#675 覆核 F2）。
#
# 為什麼不能只驗「非空」：`RETIRED_RULE_XMLIDS` 是「fresh install 不會重建那幾條退役
# rule」的**唯一**機制，而本檔幾乎每一支②的測試都是拿 `rl.RETIRED_RULE_XMLIDS` 自己
# 當迴圈的來源 ⇒ 從清單裡拿掉一個名字，那些迴圈只是**少跑一輪**，總數仍 > 0、輸出與
# 全部通過完全相同。覆核者實測：
#
#     拿掉 #658 新加的兩個條目 → repo-lint EXIT=0、unittest Ran 52 tests OK   ← 零訊號
#     對照：拿掉 #556 那兩個條目 → 同樣 52 tests OK（舊條目也沒被釘住）
#     反向對照：SECURITY_RULE_NOUPDATE_ALLOWED 塞回一條 → FAILED (failures=9)  ← 那一側有守住
#
# 危險在於後果**不對稱**、而且兩層防禦可以被**單一事件**同時解除：清單與
# `src/security/ir_rules.xml` 的 `<record>` 是同一張 PR 動的兩個檔，一次 rebase／merge
# 還原就可能同時帶回 `<record>` 並帶走清單條目 ⇒ 已升級環境無感（migration 只跑一次），
# fresh install 靜默重建那條 rule ＝ #556／#658 的寫入洞原樣重開，沒有任何測試會紅。
# 這個 floor 住在**第三個檔**（本檔），所以那個單一事件掃不到它。
#
# 判準寫在不變式層：只釘**名字**（「這個名字被 migration DELETE 退役過，永遠不得
# 從清單消失」），**不釘**值裡的版號字串——版號在本 PR 期間被推過三次，把它凍成
# 契約等於再多一個改號時要同步的載體（而 migration 檔頭數的那份清單已經漏過）。
# 「名字 → 退役它的那支 migration」由 `retirement_migration()` 從值即時推導並驗證。
#
# ⚠ 這份 floor **只准長不准縮**。真的要讓一個名字退出列管（例如那條 rule 以新 xmlid
# 正式回歸），請連同這裡一起刪並在 PR 寫下理由——那個動作會出現在 diff 裡被覆核，
# 靜默少跑一輪不會。
RETIRED_RULE_XMLIDS_FLOOR = (
    "workspace_access_rule_member_access",   # #556 / migration 18.0.1.0.122
    "workspace_access_rule_create",          # #556 / migration 18.0.1.0.122
    "workspace_rule_member_access",          # #658 / migration 18.0.1.0.129
    "workspace_rule_create",                 # #658 / migration 18.0.1.0.129
)


class InjectionFailed(AssertionError):
    """注入沒有真的發生。**這是失敗，不是「測試通過」。**

    寫死錨點的注入工具在別人改排版的當天會靜默不匹配，`str.replace()` 找不到就原樣
    回傳 ⇒ 測試改為驗證一份未突變的原始碼，而且看起來完全正常（姊妹檔
    `test_repo_lint_docstring_registry.py` 有兩次實證）。所以這裡的錨點一律從真相源
    推導（record 的 xmlid），並在注入後用**與 gate 無關的獨立判準**（純字串比對）
    自檢語意真的變了——拿 gate 自己的 parser 當絆線的 oracle 等於同一把尺量兩次。
    """


# ------------------------------------------------------------ fixture 組裝

def rule_xml(xmlid, model_ref=_WA_REF, groups=(_TENANT,), true_perms=("perm_read",),
             active=None, model="ir.rule", quote='"', omit_perms=False,
             model_ref_via_eval=False):
    """組一顆 `<record model="ir.rule">`。

    `groups=None` ＝**不宣告** groups 欄位（Odoo 的 global rule，對所有人生效）；
    `groups=()` ＝宣告了但一個 ref 都沒有。兩者語意不同，fixture 必須表達得出來。
    `omit_perms=True` ＝完全不宣告 perm_*（Odoo 的 `default=True`，四個方向都套用）。
    """
    q = quote
    out = ["        <record id=%s%s%s model=%s%s%s>" % (q, xmlid, q, q, model, q)]
    out.append('            <field name="name">%s</field>' % xmlid)
    if model_ref is not None:
        if model_ref_via_eval:
            out.append('            <field name="model_id" eval="ref(\'%s\')"/>' % model_ref)
        else:
            out.append('            <field name="model_id" ref="%s"/>' % model_ref)
    out.append("            <field name=\"domain_force\">[(1, '=', 1)]</field>")
    if groups is not None:
        refs = ", ".join("(4, ref('%s'))" % g for g in groups)
        out.append('            <field name="groups" eval="[%s]"/>' % refs)
    if active is not None:
        out.append('            <field name="active" eval="%s"/>' % active)
    if not omit_perms:
        for perm in _PERMS:
            out.append('            <field name="%s" eval="%s"/>'
                       % (perm, "True" if perm in true_perms else "False"))
    out.append("        </record>")
    return "\n".join(out)


def grandfathered_block():
    """真檔案裡那個 `<data noupdate="1">` 區塊的等價物（#658 的兩條）。

    合成 fixture 預設**都要帶上它**：① 的 grandfather 對帳是**雙向**的，清單項找不到
    對應 record 時它會（正確地）報違規 ⇒ 一份不含這兩條的 fixture 會一律多兩條與被測
    形態無關的違規。與其在斷言裡把那兩條濾掉（濾掉就等於把守線關掉一半），不如讓
    fixture 忠實模型化現實。要驗反向那條的測試顯式傳 `grandfathered=False`。
    """
    records = [rule_xml(xmlid, model_ref="model_woow_paas_platform_workspace",
                        true_perms=("perm_read", "perm_write"))
               for xmlid in sorted(rl.SECURITY_RULE_NOUPDATE_ALLOWED)]
    return '    <data noupdate="1">\n%s\n    </data>' % "\n\n".join(records)


# `data_noupdate` 的兩個特殊值——三種佈局都是 Odoo 合法的，而它們的 noupdate 判定
# 各不相同，所以 fixture 要表達得出來（只認 `<data noupdate="1">` 的判準會被另外兩種
# 整條繞過）。
NO_DATA_BLOCK = None          # 完全不包 <data>，record 直接掛 <odoo>
DATA_WITHOUT_NOUPDATE = "<data-no-attr>"   # 包 <data> 但**不宣告** noupdate ⇒ 繼承外層


def data_file(records, data_noupdate="0", root_noupdate=None, prologue="",
              grandfathered=True):
    """把 record 包成一份可 parse 的 data XML。

    `data_noupdate` 吃三種值：具體字串（`<data noupdate="…">`）、`NO_DATA_BLOCK`
    （沒有 `<data>` 包層，record 直接掛在 `<odoo>` 底下）、`DATA_WITHOUT_NOUPDATE`
    （有 `<data>` 但不宣告該屬性 ⇒ 繼承外層，對齊 `nodeattr2bool` 的 default）。
    """
    root_attr = "" if root_noupdate is None else ' noupdate="%s"' % root_noupdate
    blocks = [grandfathered_block()] if grandfathered else []
    body = "\n\n".join(records)
    if data_noupdate is NO_DATA_BLOCK:
        blocks.append(body)
    elif data_noupdate == DATA_WITHOUT_NOUPDATE:
        blocks.append("    <data>\n%s\n    </data>" % body)
    else:
        blocks.append('    <data noupdate="%s">\n%s\n    </data>'
                      % (data_noupdate, body))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<odoo%s>\n%s%s\n</odoo>\n'
            % (root_attr, prologue, "\n\n".join(blocks)))


def tenant_manage_rule(xmlid="workspace_access_rule_owner_admin_manage"):
    """涵蓋三個寫入方向、且對租戶生效的那顆（真檔案裡的形狀）。"""
    return rule_xml(xmlid, true_perms=("perm_read",) + tuple(_DIRECTIONS))


def admin_full_rule(xmlid="workspace_access_rule_admin_full"):
    """只掛 group_system 的 admin bypass——三個寫入方向都 True，但對租戶不生效。"""
    return rule_xml(xmlid, groups=(_ADMIN_GROUP,),
                    true_perms=("perm_read",) + tuple(_DIRECTIONS))


def member_read_rule(xmlid="workspace_access_rule_member_read"):
    return rule_xml(xmlid, true_perms=("perm_read",))


class Fixture(object):
    """暫存假 repo root。真 repo 一個字都不會被動到。"""

    def __init__(self, case, text=None, rel=_REAL_RULES_REL):
        self.root = tempfile.mkdtemp(prefix="repo-lint-659-")
        case.addCleanup(shutil.rmtree, self.root, True)
        self.rel = rel
        if text is not None:
            self.write(rel, text)

    @classmethod
    def from_real(cls, case):
        """基準 fixture ＝真檔案的複本（手抄的 fixture 會與真實佈局脫鉤）。"""
        return cls(case, _REAL_RULES.read_text(encoding="utf-8"))

    # -------------------------------------------------- 讀寫
    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def text(self):
        with open(os.path.join(self.root, self.rel), encoding="utf-8") as fh:
            return fh.read()

    def write_acl(self, read="1", write="1", create="1", unlink="1"):
        """寫一份 `ir.model.access.csv`——只影響**違規訊息**，不影響 pass/fail。"""
        self.write("src/security/ir.model.access.csv", (
            "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
            "access_wa_user,woow_paas_platform.workspace_access.user,%s,%s,%s,%s,%s,%s\n"
            % (_WA_REF, _TENANT, read, write, create, unlink)))

    # -------------------------------------------------- 被測 check
    def noupdate(self):
        return rl.check_security_rule_noupdate(self.root)

    def retired(self):
        return rl.check_retired_rule_xmlid(self.root)

    def directions(self):
        return rl.check_workspace_access_rule_directions(self.root)

    def all_violations(self):
        return self.noupdate() + self.retired() + self.directions()

    # -------------------------------------------------- 注入（自帶絆線）
    def _record_span(self, xmlid):
        """指定 xmlid 那顆 `<record>` 的 [start, end)；錨點從真相源（xmlid）推導。"""
        text = self.text()
        needle = '<record id="%s"' % xmlid
        start = text.find(needle)
        if start < 0:
            raise InjectionFailed(
                "fixture 裡找不到 `%s` 的 <record> 宣告——注入工具的錨點失效"
                "（真檔案改了形狀？），**不是測試通過**" % xmlid)
        close = text.find("</record>", start)
        if close < 0:
            raise InjectionFailed("`%s` 的 <record> 沒有結尾標籤——fixture 壞掉" % xmlid)
        return text, start, close + len("</record>")

    def drop_record(self, xmlid):
        """整顆 record 移掉，並自檢它真的不見了（獨立判準：純字串比對）。"""
        text, start, end = self._record_span(xmlid)
        mutated = text[:start] + text[end:]
        if mutated == text:
            raise InjectionFailed("移除 `%s` 之後原始碼一字未變——注入失效" % xmlid)
        self.write(self.rel, mutated)
        if '<record id="%s"' % xmlid in self.text():
            raise InjectionFailed("`%s` 的宣告還在——注入沒生效，測試會假綠" % xmlid)
        return self

    def flip_perm(self, xmlid, perm, value="False"):
        """把某顆 record 的某個 perm 位翻掉；**只動那一顆**，並自檢語意真的變了。"""
        text, start, end = self._record_span(xmlid)
        block = text[start:end]
        old = 'name="%s" eval="True"' % perm
        new = 'name="%s" eval="%s"' % (perm, value)
        if old not in block:
            raise InjectionFailed(
                "`%s` 裡找不到 `%s`（該 record 目前的內容不含這個 True 的 perm 位）"
                "——注入工具的錨點失效，不是測試通過" % (xmlid, old))
        mutated_block = block.replace(old, new)
        self.write(self.rel, text[:start] + mutated_block + text[end:])
        after, s2, e2 = self._record_span(xmlid)
        if new not in after[s2:e2] or old in after[s2:e2]:
            raise InjectionFailed("`%s.%s` 沒有被翻成 %s——注入沒生效" % (xmlid, perm, value))
        return self

    def append_noupdate_block(self, record):
        """**自己造**一個 `<data noupdate="1">` 區塊（掛在 `</odoo>` 之前）再放 record。

        🔴 與 `insert_into_noupdate_block` 的差別是錨點的層次，這正是 #658 撞到的 F3：
        那一支從「樹上**現有**的 noupdate 區塊」推導錨點，而「樹上有這種區塊」是
        #658 之前的**形態**——#658 的整個目的就是消滅它 ⇒ 錨點消失、注入靜默不發生、
        gate 回 0 條，測試讀成「gate 沒抓到」。判準必須錨在不變式（「gate 對
        noupdate 區塊裡的 ir.rule 必須報違規」），不是錨在「這棵樹現在長什麼樣」。

        `</odoo>` 是 XML 結構的必然，不是佈局選擇，所以它是安全的錨點。
        """
        text = self.text()
        anchor = "</odoo>"
        if anchor not in text:
            raise InjectionFailed(
                "fixture 沒有 `%s` 收尾——這不是佈局差異，是檔案壞了" % anchor)
        block = '    <data noupdate="1">\n%s\n    </data>\n\n' % record
        mutated = text.replace(anchor, block + anchor, 1)
        if mutated == text:
            raise InjectionFailed("插入後原始碼一字未變——注入失效")
        self.write(self.rel, mutated)
        # 絆線：用**與 gate 無關的獨立判準**（純字串）自檢注入真的發生了。少了這一段，
        # 「gate 沒抓到」與「東西根本沒注入成功」的輸出都是 `0 != 1`，分不出來。
        after = self.text()
        if '<data noupdate="1">' not in after:
            raise InjectionFailed("noupdate 區塊沒有寫進去——注入沒生效，測試會假綠")
        if record.strip().splitlines()[0].strip() not in after:
            raise InjectionFailed("注入的 record 沒有出現在檔案裡——注入沒生效")
        return self

    def insert_into_noupdate_block(self, record):
        """把一顆 record 插進**第一個 `<data noupdate="1">` 區塊**的結尾。

        ⚠ 錨點是「樹上已經有這種區塊」——真檔案在 #658 之後**沒有**了，所以這支只
        適用於合成 fixture（`data_file(...)` 預設會帶一個）。要對真檔案注入請改用
        `append_noupdate_block()`。
        """
        text = self.text()
        opener = '<data noupdate="1">'
        start = text.find(opener)
        if start < 0:
            raise InjectionFailed(
                "fixture 裡沒有 `%s` 區塊——注入工具的假設不成立（#658 修好之後這支要"
                "改用合成 fixture）" % opener)
        close = text.find("</data>", start)
        if close < 0:
            raise InjectionFailed("noupdate 區塊沒有結尾標籤——fixture 壞掉")
        mutated = text[:close] + record + "\n\n" + text[close:]
        if mutated == text:
            raise InjectionFailed("插入後原始碼一字未變——注入失效")
        self.write(self.rel, mutated)
        return self


def blob(violations):
    return "\n".join(violations)


def allow_temporarily(case, **entries):
    """暫時把名字塞進 ① 的 grandfather 清單，測試結束一定還原。

    #658 把真樹的 `SECURITY_RULE_NOUPDATE_ALLOWED` 清空了（那是它的目的），於是 ①
    的**兩端**——放行與反向對帳——都失去現實驅動者。清空是好事，但守線的分支不能因此
    變成沒人跑過的死碼，所以改用合成驅動。

    用 `addCleanup` 還原**內容**而不是重新綁一個新 dict：`repo-lint.py` 裡的判準讀的
    是模組層那個物件本身，換掉綁定會讓還原對已經取得參照的地方失效。
    """
    original = dict(rl.SECURITY_RULE_NOUPDATE_ALLOWED)

    def restore():
        rl.SECURITY_RULE_NOUPDATE_ALLOWED.clear()
        rl.SECURITY_RULE_NOUPDATE_ALLOWED.update(original)

    case.addCleanup(restore)
    rl.SECURITY_RULE_NOUPDATE_ALLOWED.update(entries)


def anchor_temporarily(case, **constants):
    """暫時改掉 `repo-lint.py` 的 model 錨點常數（`WORKSPACE_MODEL_REF` 等），測試結束一定還原。

    改的是**模組物件上的字串屬性**，不是就地改原始碼再重新 import ⇒ 不吃檔頭那段
    `.pyc` stale cache 的陷阱（那個陷阱只咬後者）。兩條 check 在**呼叫當下**才讀這
    兩個模組層常數，所以 setattr 這種改法真的會影響判定——這件事本身由下面
    `test_the_anchor_patch_actually_reaches_the_gate` 當絆線釘住。

    改完立刻用**與被測 gate 無關的獨立判準**（直接讀回屬性）自檢：少了它，「patch 沒
    生效」與「gate 沒抓到」的輸出完全相同，而這正是本檔絆線哲學要防的那一格。
    """
    original = {name: getattr(rl, name) for name in constants}

    def restore():
        for name, value in original.items():
            setattr(rl, name, value)

    case.addCleanup(restore)
    for name, value in constants.items():
        setattr(rl, name, value)
        if getattr(rl, name) != value:
            raise InjectionFailed(
                "`rl.%s` 沒有被改成 %r——注入失效，**不是測試通過**" % (name, value))


# ------------------------------------------------------------ 陽性對照（現實錨點）

class TestCleanTreeIsGreen(unittest.TestCase):
    """特異性的地基：真 repo 現況必須綠，而且**綠得有前提**。"""

    def test_real_repo_is_green(self):
        for name, got in (("noupdate", rl.check_security_rule_noupdate(str(_REPO_ROOT))),
                          ("retired", rl.check_retired_rule_xmlid(str(_REPO_ROOT))),
                          ("directions", rl.check_workspace_access_rule_directions(str(_REPO_ROOT)))):
            with self.subTest(check=name):
                self.assertEqual(got, [], "真 repo 現況不該有違規（%s）" % name)

    def test_the_scan_actually_saw_ir_rules(self):
        """「零違規」若來自空集合就是假綠——釘住取樣層的前提。"""
        records, errors = rl.module_record_declarations(str(_REPO_ROOT))
        self.assertEqual(errors, [], "有 XML parse 不了，後面的判定都不可信")
        rules = [r for r in records if r["model"] == rl.IR_RULE_MODEL]
        self.assertGreaterEqual(len(rules), 20,
                                "掃到的 ir.rule 太少——**探針沒走到 repo，不是 repo 變小了**")

    def test_the_grandfather_allowlist_and_the_tree_agree_and_are_empty(self):
        """🔴 ① 的 grandfather 清單與真樹的 noupdate 區塊必須**互相一致，而且都是空的**。

        這一支原本斷言的是相反的事——「真檔案裡確實有 noupdate 區塊的 ir.rule」。
        那是 #658 之前的**形態**，而 #658 的整個目的就是消滅它，所以它在修好的當天
        必然變紅（#658 撞到的 F2）。它的失敗訊息自己就寫著「#658 修好了嗎？那就把
        grandfather 清單清掉」——作者預見到這一刻，只是沒寫下清掉之後該長什麼樣。

        清掉之後該長的樣子就是這一支：**把「已經清乾淨」變成受監視的不變式**。
        空清單是好事，但「空」本身要有東西盯著，否則下一個人加回一條沒人會發現
        （而 ① 對清單裡的名字是**放行**的，加回去就是靜默豁免）。

        判準保留了原本那條**雙向一致性**斷言（清單 == 樹上實況），只是現在兩邊都應為
        空集合；它仍然抓得到「有人只改一邊」。放行分支本身不靠這一支驅動——它現在由
        `test_grandfathered_records_are_allowed` 用**合成**驅動（見那一支的 docstring）。
        """
        records, _ = rl.module_record_declarations(str(_REPO_ROOT))
        in_noupdate = {r["xmlid"] for r in records
                       if r["model"] == rl.IR_RULE_MODEL and r["noupdate"]}
        self.assertEqual(
            in_noupdate, set(rl.SECURITY_RULE_NOUPDATE_ALLOWED),
            "noupdate 區塊的 ir.rule 與 grandfather 清單不相等——① 應該已經在報違規了，"
            "這支的前提與 gate 的判定分岔了")
        self.assertEqual(
            in_noupdate, set(),
            "真樹的 noupdate 區塊又住進了 ir.rule。ir_model_data.noupdate 是 sticky 的："
            "寫錯的授權規則不能用改 XML 修好，只能下 migration DELETE 退役（#556 的可修復性"
            "根因、#658 的 workspace 側）。要新增請走 `<data noupdate=\"0\">`；真的必須 "
            "noupdate 才把名字加進 SECURITY_RULE_NOUPDATE_ALLOWED 並寫下理由——**而且要記得"
            "同時更新這一支**，因為它現在盯的就是「這裡是空的」")

    def test_retired_names_still_live_in_code_and_are_not_flagged(self):
        """特異性的真實靶子：退役名字今天仍**合法**以文字形式存在。

        migration 的 `_RETIRED` tuple 是那些名字的權威載體（它就是靠名字去 DELETE
        的），永遠會留著。字串判準會把它當違規；② 判的是「有沒有 `<record id=…>`
        宣告」，所以必須綠。

        🔴 判準從「形態」搬到「不變式」（#658 撞到的 F1）：原本寫死讀
        `src/migrations/18.0.1.0.122/post-migrate.py`——那是當時**唯一**的退役事件。
        #658 在 `18.0.1.0.129` 退役了第二批，那兩個名字當然不在 122 裡，於是這支報
        「名字不在 migration 裡了」，把讀的人導向「退役機制被改壞了」這個錯誤方向。

        不變式是：**每個退役名字，都能在「退役它的那支」migration 裡找到**。
        該去哪一支由 `RETIRED_RULE_XMLIDS` 的值自己說（版號就在值裡），所以第三次、
        第四次退役都不必回來改這支。
        """
        missing = [x for x in RETIRED_RULE_XMLIDS_FLOOR
                   if x not in rl.RETIRED_RULE_XMLIDS]
        self.assertEqual(
            missing, [],
            "RETIRED_RULE_XMLIDS 少了這些名字：%s。它們各自被一支 migration DELETE "
            "退役過，而這份清單是「fresh install 不會重建它們」的**唯一**機制——少一個"
            "就是那條授權 rule 在新裝環境靜默復活（已升級環境無感，沒有任何測試會紅）。"
            "⚠ 這裡刻意列舉名字而不是 `assertTrue(非空)`：下面的迴圈是拿清單自己當"
            "來源的，少一個名字只會**少跑一輪**，輸出與全部通過一模一樣"
            "（#675 覆核 F2 實測：拿掉兩個條目 → repo-lint EXIT=0、52 tests OK）"
            % (missing,))
        for xmlid, provenance in sorted(rl.RETIRED_RULE_XMLIDS.items()):
            with self.subTest(xmlid=xmlid):
                path = retirement_migration(provenance)
                # 絆線：先分辨「版號解析不出來／migration 不存在」與「名字不在裡面」。
                # 少了這兩道，三種病因的訊息會全部長成「名字不在 migration 裡」。
                self.assertIsNotNone(
                    path,
                    "RETIRED_RULE_XMLIDS['%s'] = %r 解析不出版號——值的格式應為"
                    "「版號（#issue）」" % (xmlid, provenance))
                self.assertTrue(
                    path.is_file(),
                    "RETIRED_RULE_XMLIDS['%s'] = %r 指向 %s，但那支 migration 不存在"
                    "——版號寫錯、或 migration 被改名／刪掉（**不是**「名字找不到」）"
                    % (xmlid, provenance, path.relative_to(_REPO_ROOT)))
                self.assertIn(
                    xmlid, path.read_text(encoding="utf-8"),
                    "%s 不在 %s 裡——退役它的那支 migration 沒有提到它，退役機制與"
                    "清單分岔了" % (xmlid, path.relative_to(_REPO_ROOT)))
        self.assertEqual(rl.check_retired_rule_xmlid(str(_REPO_ROOT)), [])

    def test_a_faithful_copy_of_the_real_file_is_green(self):
        """基準 recipe：fixture 複本必須與真 repo 同判定（比對實驗要固定 recipe）。"""
        self.assertEqual(Fixture.from_real(self).all_violations(), [])


# ------------------------------------------------------------ ① noupdate 出生地

class TestNoupdateBlockInjection(unittest.TestCase):
    """敏感度①：ir.rule 落在 noupdate="1" 區塊必須紅且點名 xmlid。"""

    def test_a_new_rule_in_a_synthesised_noupdate_block_is_red_then_green(self):
        """真檔案的複本 ＋ **自己造**的 noupdate 區塊 → 必須紅且點名 → 還原 → 綠。

        🔴 錨點從形態層搬到不變式層（#658 撞到的 F3）：原本是往「真檔案裡**現有**的
        `<data noupdate="1">` 區塊」注入，而 #658 把那個區塊整個消滅了 ⇒ 注入工具的
        錨點失效、`insert_into_noupdate_block` 找不到區塊、gate 回 0 條，測試讀成
        「gate 沒抓到」。要驗的不變式是「gate 對 noupdate 區塊裡的 ir.rule 必須報
        違規」，那跟這棵樹現在有沒有那種區塊無關，所以自己造一個。

        基準仍用真檔案複本（不是手抄 fixture）：手抄的會與真實佈局脫鉤。
        注入工具自帶絆線（見 `append_noupdate_block`），所以「gate 沒抓到」與
        「東西根本沒注入成功」分得出來——兩者原本的輸出都是 `0 != 1`。
        """
        fixture = Fixture.from_real(self)
        self.assertEqual(fixture.noupdate(), [], "注入前就紅了——基準壞掉")
        clean = fixture.text()
        # 前提：這棵樹現在沒有任何 ir.rule 住在 noupdate 區塊，所以必須自己造一個。
        # ⚠ 判準用 **parse 出來的 record**，不是字串比對 `<data noupdate="1">`——
        # 那串字今天合法地出現在 ir_rules.xml 的**註解**裡（#658 留下的檔頭說明），
        # 字串判準會在這裡誤判。這正是本檔一直在講的「形態層 vs 不變式層」，
        # 寫這支的時候我自己先踩了一次。
        records, _ = rl.module_record_declarations(fixture.root)
        self.assertEqual(
            [r["xmlid"] for r in records
             if r["model"] == rl.IR_RULE_MODEL and r["noupdate"]], [],
            "真檔案的複本裡已經有 noupdate 區塊的 ir.rule——這支的『自己造一個』前提要"
            "重新檢視（而且 test_the_grandfather_allowlist_and_the_tree_agree_and_are_empty"
            " 應該已經紅了）")
        fixture.append_noupdate_block(
            rule_xml("injected_rule_for_gate_test", model_ref="model_woow_paas_platform_workspace"))
        got = fixture.noupdate()
        self.assertEqual(len(got), 1, "應該恰好報一條：%s" % blob(got))
        self.assertIn("injected_rule_for_gate_test", got[0],
                      "違規訊息沒點名是哪一個 xmlid——「有東西壞了」是沒法行動的訊號")
        self.assertIn('noupdate="1"', got[0])
        # 還原後綠：證明紅是被注入打中，不是 fixture／環境壞掉。
        fixture.write(fixture.rel, clean)
        self.assertEqual(fixture.noupdate(), [])

    def test_attribute_order_and_single_quotes_are_still_caught(self):
        """`<record model='ir.rule' id='x'>`——regex 判準最容易在這裡靜默零命中。"""
        fixture = Fixture(self, data_file(
            [rule_xml("sneaky_rule", quote="'")], data_noupdate="1"))
        got = fixture.noupdate()
        self.assertEqual(len(got), 1, "屬性順序／單引號形態沒被抓到：%s" % blob(got))
        self.assertIn("sneaky_rule", got[0])

    def test_noupdate_on_the_odoo_root_without_a_data_block_is_caught(self):
        """`<odoo noupdate="1">` ＋裸 record：只認 `<data>` 的判準會整條被繞過。"""
        fixture = Fixture(self, data_file(
            [rule_xml("bare_root_rule")], data_noupdate=None, root_noupdate="1"))
        got = fixture.noupdate()
        self.assertEqual(len(got), 1,
                         "掛在 <odoo noupdate=\"1\"> 底下的裸 record 沒被抓到：%s"
                         % blob(got))
        self.assertIn("bare_root_rule", got[0])

    def test_inner_data_noupdate_zero_overrides_the_outer_one(self):
        """特異性：內層顯式 noupdate="0" 勝出（對齊 Odoo `_tag_root` 的 stack 語意）。"""
        fixture = Fixture(self, data_file(
            [rule_xml("normal_rule")], data_noupdate="0", root_noupdate="1"))
        self.assertEqual(fixture.noupdate(), [],
                         "內層 noupdate=\"0\" 應該勝出，這是合法形態不該被擋")

    def test_a_commented_out_record_is_not_a_violation(self):
        """特異性：註解掉的舊寫法在語意上不生效（`ET.parse` 直接丟掉註解）。"""
        commented = ("    <!--\n" + rule_xml("ghost_rule") + "\n    -->\n")
        fixture = Fixture(self, data_file(
            [rule_xml("normal_rule")], data_noupdate="0", prologue=commented))
        got = fixture.noupdate()
        self.assertEqual(got, [],
                         "註解裡的假 record 被當成生效宣告——假陽性會逼人刪掉註解，"
                         "而註解正是下一個人唯一的線索：%s" % blob(got))
        # 肯定性對照：同一份 fixture 把註解拆掉（record 真的生效）就必須紅，證明上面
        # 那句綠不是因為整條 check 對這份 fixture 沒在跑。
        fixture.write(fixture.rel, data_file([rule_xml("ghost_rule")],
                                             data_noupdate="1"))
        self.assertTrue(any("ghost_rule" in v for v in fixture.noupdate()),
                        "拆掉註解後仍然綠 ⇒ 上面那句綠沒有鑑別力")

    def test_grandfathered_records_are_allowed(self):
        """① 的放行分支——**合成**驅動（#658 之後真樹的清單已經是空的）。

        🔴 這一支原本靠現實驅動（「#658 的兩條必須放行」），而 #658 把那兩條退役掉之後
        它退化成恆真：`data_file()` 的 grandfather 區塊由空清單組出來 ⇒ 區塊是空的 ⇒
        斷言變成「一份沒有違規的檔案沒有違規」。它照樣是綠的，所以不會有人發現放行
        分支已經沒有任何驅動者——**這正是 F2 那支要防的死碼，只是它自己也倒了**。

        改成自己造一顆住在 noupdate 區塊的 rule ＋ 暫時把它加進清單。
        敏感度（不在清單裡→紅且點名）與放行（在清單裡→不報它）**成對**：少了前半，
        「放行生效」與「① 對這份 fixture 根本沒在跑」的輸出完全相同。
        """
        xmlid = "synthetic_grandfathered_rule"
        fixture = Fixture(self, data_file(
            [rule_xml(xmlid, model_ref="model_woow_paas_platform_workspace")],
            data_noupdate="1", grandfathered=False))

        before = fixture.noupdate()
        self.assertTrue(
            any(xmlid in v for v in before),
            "不在 grandfather 清單裡卻沒被報 ⇒ ① 對這份 fixture 根本沒在跑，"
            "下面那句「放行」會是假綠：%s" % blob(before))

        allow_temporarily(self, **{xmlid: "合成驅動（本測試）；真樹的清單是空的"})
        after = fixture.noupdate()
        self.assertEqual(
            after, [],
            "加進 grandfather 清單後仍有違規 ⇒ 放行分支沒有生效：%s" % blob(after))

    def test_an_allowlist_entry_with_no_matching_record_is_red(self):
        """反向對帳：清單比它豁免的東西活得久 ⇒ 同名 record 日後會無聲繼承豁免。

        🔴 同樣改成合成驅動：原本斷言 `len(got) == len(SECURITY_RULE_NOUPDATE_ALLOWED)`，
        清單空掉之後那是 `0 == 0`，for 迴圈一次都不跑 ⇒ 恆真。反向對帳是 ① 的兩端
        之一（另一端是放行），兩端都必須有驅動者。
        """
        ghost = "allowlisted_but_absent_rule"
        fixture = Fixture(self, data_file([tenant_manage_rule()], data_noupdate="0",
                                          grandfathered=False))
        self.assertEqual(fixture.noupdate(), [], "注入前就紅了——基準壞掉")

        allow_temporarily(self, **{ghost: "合成驅動（本測試）"})
        got = fixture.noupdate()
        self.assertEqual(len(got), 1,
                         "清單項沒有對應 record 時該恰好報一條：%s" % blob(got))
        self.assertIn(ghost, got[0], "訊息沒點名是哪一個清單項——沒法行動的訊號")

    def test_only_the_offending_record_is_named(self):
        """點名精確度：grandfather 兩條在同一份檔裡照樣放行，只報新增那一條。"""
        fixture = Fixture(self, data_file([rule_xml("brand_new_rule")],
                                          data_noupdate="1"))
        got = fixture.noupdate()
        self.assertEqual(len(got), 1, blob(got))
        self.assertIn("brand_new_rule", got[0])
        for allowed in rl.SECURITY_RULE_NOUPDATE_ALLOWED:
            self.assertNotIn(allowed, got[0],
                             "放行清單裡的 record 被一起點名了——訊息會把人導向錯的地方")

    def test_a_rule_in_a_noupdate_zero_block_is_green(self):
        """特異性：正常宣告（noupdate="0"）不得被誤擋。"""
        fixture = Fixture(self, data_file(
            [rule_xml("brand_new_rule"), tenant_manage_rule()], data_noupdate="0"))
        self.assertEqual(fixture.noupdate(), [])


# ------------------------------------------------------------ ① noupdate 值語意（F1）

# 逐字抄自 `odoo/tools/convert.py`：`str2bool(value)` ＝
# `value.lower() not in ('0', 'false', 'off')`（**純黑名單**，永不 raise），
# `nodeattr2bool` 先 `strip()`、空字串回 default（＝繼承外層）。
# 🔴 convert.py **自己定義**這個 str2bool，沒有 import `misc.py` 那份白名單版
# （該檔只從 misc import file_open / file_path / SKIPPED_ELEMENT_TYPES）。#659 驗收 F1
# 就是照 misc.py 那份寫造成的：`<data noupdate="no">` 在 Odoo 是 sticky、gate 卻放行。
#
# 表的出處要重新核對時**別照抄本註解**：用 `ast` 從真的 convert.py 抽出 str2bool 的
# 函式原文再 exec，餵值比對（實測 odoo-17.0 與 odoo-18.0 兩份樹逐字相同）。這裡不直接
# 讀 Odoo 原始碼是因為 CI 容器沒有那棵樹，而本檔的契約是零外部依賴。
_NOUPDATE_STICKY = ("1", "true", "True", "TRUE", "yes", "Yes", "on", "t", "y",
                    "no", "No", "NO", "f", "n", "banana", " 1 ", " no ")
_NOUPDATE_NOT_STICKY = ("0", "false", "False", "off", "OFF", " 0 ")


class TestNoupdateValueSemantics(unittest.TestCase):
    """helper 層：`noupdate` 的值判定必須逐字對齊 convert.py 的黑名單語意。"""

    def test_sticky_values(self):
        for raw in _NOUPDATE_STICKY:
            with self.subTest(noupdate=raw):
                self.assertTrue(rl._noupdate_attr_is_true(raw),
                                "Odoo 的 str2bool 對 %r 回 True（只有 0/false/off 是 "
                                "False）——判 False 就是一條靜默放行路徑" % raw)

    def test_not_sticky_values(self):
        for raw in _NOUPDATE_NOT_STICKY:
            with self.subTest(noupdate=raw):
                self.assertFalse(rl._noupdate_attr_is_true(raw),
                                 "%r 在 Odoo 是 noupdate=0，判 True 會誤擋正常宣告" % raw)

    def test_the_two_boolean_paths_are_separate_functions(self):
        """`noupdate`（XML 屬性）與 `perm_*`（field eval）在 Odoo 是不同函式。

        共用一個 helper 正是 F1 的成因，所以這裡釘住「兩個函式都存在」。刻意**不**
        斷言兩者對某個值的判定不同——`_field_literal_bool` 的 text 形態偏差是已知的
        過嚴誤報（見它的 docstring），把偏差寫成契約會讓未來真的去對齊時被自己的測試擋。
        """
        self.assertTrue(callable(rl._noupdate_attr_is_true))
        self.assertTrue(callable(rl._field_literal_bool))

    def test_field_literal_bool_handles_safe_eval_literals(self):
        for raw, want in (("True", True), ("1", True), ("False", False), ("0", False)):
            with self.subTest(raw=raw):
                self.assertIs(rl._field_literal_bool(raw, None), want)


class TestNoupdateNegativeSideIsNotAnEscape(unittest.TestCase):
    """端到端：Odoo 視為 sticky 的**否定側**寫法不得放行（#659 驗收 F1 的回歸測試）。"""

    def _probe(self, data_noupdate, root_noupdate=None):
        return Fixture(self, data_file([rule_xml("f1_probe_rule")],
                                       data_noupdate=data_noupdate,
                                       root_noupdate=root_noupdate))

    def test_every_sticky_spelling_is_red_and_prints_the_actual_value(self):
        for raw in ("no", "No", "NO", "f", "n", "banana"):
            with self.subTest(noupdate=raw):
                got = self._probe(raw).noupdate()
                self.assertEqual(len(got), 1,
                                 "<data noupdate=%r> 在 Odoo 是 sticky noupdate，gate "
                                 "放行＝一條確定可用的繞過路徑：%s" % (raw, blob(got)))
                self.assertIn("f1_probe_rule", got[0])
                # 訊息必須印**實際**寫在檔裡的值：寫死 noupdate="1" 會叫讀者去 grep 一個
                # 檔案裡不存在的字串（同 F3 那族——訊息斷言它沒驗過的東西）。
                self.assertIn('noupdate="%s"' % raw, got[0],
                              "訊息沒印出實際的 noupdate 值：%s" % got[0])

    def test_inheritance_from_the_odoo_root_is_red(self):
        """`<odoo noupdate="no">` 的兩種繼承佈局都必須紅。

        (a) 裸 record（沒有 `<data>` 包層）；(b) 有 `<data>` 但**不宣告** noupdate
        ⇒ `nodeattr2bool` 回 default ＝繼承外層。verify659 的 F1 報告指名的就是 (b)。
        """
        for layout, data_noupdate in (("裸 record", NO_DATA_BLOCK),
                                      ("<data> 不宣告", DATA_WITHOUT_NOUPDATE)):
            with self.subTest(layout=layout):
                got = self._probe(data_noupdate, root_noupdate="no").noupdate()
                self.assertEqual(len(got), 1,
                                 "%s 沒有繼承 <odoo noupdate=\"no\">：%s"
                                 % (layout, blob(got)))
                self.assertIn("f1_probe_rule", got[0])
                self.assertIn('noupdate="no"', got[0])

    def test_inheritance_of_a_non_sticky_root_stays_green(self):
        """陽性對照：`<odoo noupdate="0"><data>`（不宣告）繼承 False，不得被擋。"""
        for data_noupdate in (NO_DATA_BLOCK, DATA_WITHOUT_NOUPDATE):
            with self.subTest(data_noupdate=data_noupdate):
                self.assertEqual(
                    self._probe(data_noupdate, root_noupdate="0").noupdate(), [])

    def test_non_sticky_spellings_stay_green(self):
        """陽性對照：Odoo 視為 noupdate=0 的寫法不得變紅（防過度修正）。"""
        for raw in ("0", "false", "False", "off", "OFF"):
            with self.subTest(noupdate=raw):
                self.assertEqual(self._probe(raw).noupdate(), [],
                                 "noupdate=%r 是正常宣告，不該被擋" % raw)

    def test_a_blank_attribute_falls_back_to_the_inherited_value(self):
        """`noupdate=""`／全空白 ⇒ `nodeattr2bool` 回 default ＝繼承外層，不是 False。"""
        self.assertEqual(self._probe("").noupdate(), [],
                         "外層沒有宣告 ⇒ 繼承 False ⇒ 該綠")
        got = self._probe("   ", root_noupdate="1").noupdate()
        self.assertTrue(any("f1_probe_rule" in v for v in got),
                        "空白 noupdate 應該繼承外層的 1，而不是被當成 0：%s" % blob(got))


# ------------------------------------------------------------ ② 退役 xmlid

class TestRetiredXmlidInjection(unittest.TestCase):
    """敏感度②：退役 xmlid 以 `<record>` 復活必須紅。"""

    def _fixture_with(self, xmlid, **kwargs):
        return Fixture(self, data_file(
            [rule_xml(xmlid, **kwargs), tenant_manage_rule()], data_noupdate="0"))

    def test_resurrected_record_is_red_then_green(self):
        fixture = Fixture.from_real(self)
        self.assertEqual(fixture.retired(), [], "注入前就紅了——基準壞掉")
        clean = fixture.text()
        retired = sorted(rl.RETIRED_RULE_XMLIDS)[0]
        text = fixture.text()
        anchor = '<record id="workspace_access_rule_member_read"'
        if anchor not in text:
            raise InjectionFailed("找不到插入錨點 %s——注入工具失效" % anchor)
        fixture.write(fixture.rel,
                      text.replace(anchor, rule_xml(retired) + "\n\n" + anchor, 1))
        got = fixture.retired()
        self.assertEqual(len(got), 1, blob(got))
        self.assertIn(retired, got[0])
        self.assertIn(rl.RETIRED_RULE_XMLIDS[retired], got[0],
                      "訊息沒帶退役的 migration 版號——讀的人查不到來歷")
        fixture.write(fixture.rel, clean)
        self.assertEqual(fixture.retired(), [])

    def test_module_prefixed_form_is_also_red(self):
        """`<record id="woow_paas_platform.x">` 與 `id="x"` 在 Odoo 眼裡是同一個。"""
        retired = sorted(rl.RETIRED_RULE_XMLIDS)[0]
        fixture = self._fixture_with(rl.MODULE_XMLID_PREFIX + retired)
        got = fixture.retired()
        self.assertEqual(len(got), 1, "帶 module 前綴的形態沒被抓到：%s" % blob(got))
        self.assertIn(retired, got[0])

    def test_a_different_model_reusing_the_name_is_red(self):
        """退役的語意是「這個名字整個不再使用」——換 model 一樣佔回 ir_model_data。"""
        retired = sorted(rl.RETIRED_RULE_XMLIDS)[0]
        fixture = self._fixture_with(retired, model="ir.model.access", model_ref=None)
        self.assertTrue(any(retired in v for v in fixture.retired()))

    def test_only_a_comment_mentioning_the_name_is_green(self):
        """🔴 特異性核心：現況就有註解提到那些名字，字串判準會誤報。

        🔴 註解改成吃**任意條數**（#658 撞到的 E1）：原本的 format 字串寫死兩個
        `%s`，把「退役清單恰好有 2 條」凍成契約；#658 加到 4 條的當天它拋
        `TypeError: not all arguments converted`——那是個**看起來像程式壞了**的訊息，
        完全指不出真正的成因（清單長大了）。條數是會長的，不是不變式。

        絆線：先斷言註解真的帶上了**每一個**名字。少了它，若註解的組法哪天靜默漏掉
        某些名字，這支照樣綠——而它本來要驗的就是「那些名字出現在註解裡不會被誤報」，
        名字沒出現就等於沒驗到。
        """
        names = sorted(rl.RETIRED_RULE_XMLIDS)
        self.assertTrue(names, "退役清單是空的——這支失去靶子（見 F1 那支的同款斷言）")
        note = ("    <!-- 退役紀錄（各自的 migration 見 RETIRED_RULE_XMLIDS）：%s -->\n"
                % "、".join(names))
        for xmlid in names:
            self.assertIn(xmlid, note,
                          "註解沒帶上 %s——fixture 沒有涵蓋它，這支對它零鑑別力" % xmlid)

        fixture = Fixture(self, data_file([tenant_manage_rule()],
                                          data_noupdate="0", prologue=note))
        self.assertEqual(fixture.retired(), [],
                         "只在註解裡提到退役名字被當成違規——那會逼人刪掉史料")
        # 肯定性對照：同一份 fixture 加上真的 <record> 就必須紅（證明上面的綠不是
        # 因為整條 check 對這份 fixture 沒在跑）。**逐一**驗每個名字，這樣清單長大時
        # 新加的那個也真的被驅動到，而不是永遠只測 sorted()[0] 那一個。
        for retired in names:
            with self.subTest(xmlid=retired):
                fixture.write(fixture.rel, data_file(
                    [rule_xml(retired), tenant_manage_rule()],
                    data_noupdate="0", prologue=note))
                self.assertTrue(any(retired in v for v in fixture.retired()),
                                "加了真的 <record> 卻還是綠 ⇒ 上面那句綠沒有鑑別力")

    def test_every_floor_name_is_individually_blocked_by_the_gate(self):
        """🔴 #675 覆核 F2：floor **折進迴圈**，而且迴圈的來源是 floor、不是被測清單。

        上面幾支（`test_only_a_comment_mentioning_the_name_is_green` 的肯定性對照、
        `test_resurrected_record_is_red_then_green`）確實也逐一驗過名字——但它們的
        迴圈是拿 `rl.RETIRED_RULE_XMLIDS` **自己**當來源。清單少一個名字，那些迴圈
        只是少跑一輪，`Ran 52 tests OK` 一字不變（覆核者實測：拿掉本 PR 新加的兩個
        條目、或 #556 的兩個，兩種都零訊號）。

        ⇒ 迴圈的**輪數不能由被測物自己決定**，否則被測物可以自己關掉對自己的檢查
        （memory: `feedback_declarative_list_gate_needs_floor_folded_in`，#522／#613
        同型）。這一支的輪數固定來自 `RETIRED_RULE_XMLIDS_FLOOR`，所以任何一個名字
        從 `RETIRED_RULE_XMLIDS` 消失，對應的 subTest 就會紅**並且點名是哪一個**
        （不是「差幾個」——memory:
        `feedback_completeness_check_output_which_not_how_many`）。

        兩層一起驗，缺一不可：**登記**（名字還在清單裡）與**行為**（gate 真的擋得
        下這個名字的 `<record>`）。只驗登記的話，`check_retired_rule_xmlid` 哪天被
        改成對某些名字放行也不會有訊號。
        """
        for xmlid in RETIRED_RULE_XMLIDS_FLOOR:
            with self.subTest(xmlid=xmlid):
                self.assertIn(
                    xmlid, rl.RETIRED_RULE_XMLIDS,
                    "`%s` 不在 RETIRED_RULE_XMLIDS 裡了 ⇒ fresh install 會照 "
                    "src/security/ir_rules.xml 重建這條退役 rule，而已升級環境的 "
                    "migration 只跑一次不會再刪它 ⇒ 授權洞在新裝環境無聲重開。"
                    "要恢復這條規則請走**新 xmlid ＋ 新 migration**，不要把名字從"
                    "清單拿掉（#675 覆核 F2）" % xmlid)
                fixture = self._fixture_with(xmlid)
                got = fixture.retired()
                self.assertTrue(
                    any(xmlid in v for v in got),
                    "把 `<record id=\"%s\">` 放回去，② 卻沒有報違規 ⇒ 這個名字雖然"
                    "登記著，gate 對它沒有實際攔截行為：%s" % (xmlid, blob(got)))

    def test_a_live_rule_with_a_different_name_is_green(self):
        """特異性：新 xmlid（#556 的替代規則）不得被誤擋。"""
        fixture = Fixture(self, data_file(
            [member_read_rule(), tenant_manage_rule()], data_noupdate="0"))
        self.assertEqual(fixture.retired(), [])


# ------------------------------------------------------------ ③ 方向存在性

class TestDirectionExistence(unittest.TestCase):
    """敏感度③：workspace_access 的寫入方向留空必須紅，且點名是**哪個方向**。"""

    def _directions_named(self, violations):
        return sorted(d for d in _DIRECTIONS if any(d in v for v in violations))

    def test_dropping_the_only_tenant_rule_reds_all_three_directions(self):
        fixture = Fixture.from_real(self)
        self.assertEqual(fixture.directions(), [], "注入前就紅了——基準壞掉")
        clean = fixture.text()
        fixture.drop_record("workspace_access_rule_owner_admin_manage")
        got = fixture.directions()
        self.assertEqual(self._directions_named(got), sorted(_DIRECTIONS),
                         "三個方向都該被點名：%s" % blob(got))
        self.assertEqual(len(got), 3, blob(got))
        fixture.write(fixture.rel, clean)
        self.assertEqual(fixture.directions(), [])

    def test_flipping_one_perm_reds_only_that_direction(self):
        """逐方向：只翻一個 perm 位 ⇒ 只有那一個方向該紅（一律全紅＝沒有鑑別力）。"""
        for direction in _DIRECTIONS:
            with self.subTest(direction=direction):
                fixture = Fixture.from_real(self)
                fixture.flip_perm("workspace_access_rule_owner_admin_manage", direction)
                got = fixture.directions()
                self.assertEqual(self._directions_named(got), [direction],
                                 "應該只點名 %s：%s" % (direction, blob(got)))
                self.assertEqual(len(got), 1, blob(got))

    def test_group_system_only_rule_does_not_cover_a_direction(self):
        """🔴 判準的鑑別力所在：只掛 group_system 的 admin_full **不算**涵蓋。

        Odoo `_get_rules` 的 SQL 只收 `r.global OR r.id IN (使用者所屬 group 的 rule)`
        ⇒ 一條只掛 `base.group_system` 的 rule 對純 `base.group_user` 租戶帳號完全不
        構成限制，而本 repo 每個 model 都配一條 `*_rule_admin_full` ⇒ 寬判準（「這個
        model 有沒有任一條 ir.rule」）會把「洞開著」讀成「有守」。

        這一支同時是**寬判準的陰性對照**：同一份 fixture 在寬判準下三個方向全綠。
        """
        fixture = Fixture(self, data_file(
            [member_read_rule(), admin_full_rule()], data_noupdate="0"))
        got = fixture.directions()
        self.assertEqual(self._directions_named(got), sorted(_DIRECTIONS),
                         "只剩 group_system 的 admin_full 時三個方向都該紅：%s" % blob(got))
        self.assertIn(_ADMIN_GROUP, got[0],
                      "訊息沒說明「admin_full 不算涵蓋」——下一個人的最短修法就是補一條"
                      "admin_full 讓 gate 轉綠，而洞還在")

        # 寬判準（任一條 ir.rule 即算有守）在同一份 fixture 上的判定：全綠。
        records, _ = rl.module_record_declarations(fixture.root)
        loose = {}
        for direction in _DIRECTIONS:
            loose[direction] = [
                r["xmlid"] for r in records
                if r["model"] == rl.IR_RULE_MODEL
                and rl._rule_model_ref(r["fields"]) == _WA_REF
                and rl._rule_flag(r["fields"], direction)]
        self.assertTrue(all(loose[d] for d in _DIRECTIONS),
                        "寬判準居然也紅了——那本支證明的鑑別力差距不存在，判準設計的"
                        "理由要重寫")

        # 補上一條對租戶生效的 manage rule 就必須轉綠（肯定性對照）。
        fixture.write(fixture.rel, data_file(
            [member_read_rule(), admin_full_rule(), tenant_manage_rule()],
            data_noupdate="0"))
        self.assertEqual(fixture.directions(), [])

    def test_a_global_rule_without_groups_counts(self):
        """特異性：沒宣告 groups ＝ global rule，對所有人 AND 生效 ⇒ 算涵蓋。"""
        fixture = Fixture(self, data_file(
            [rule_xml("global_rule", groups=None,
                      true_perms=("perm_read",) + tuple(_DIRECTIONS))],
            data_noupdate="0"))
        self.assertEqual(fixture.directions(), [])

    def test_an_inactive_rule_does_not_count(self):
        """`active=False` 的 rule 在 `_get_rules` 的 SQL 裡就被濾掉了。"""
        fixture = Fixture(self, data_file(
            [tenant_manage_rule(), rule_xml(
                "workspace_access_rule_dead", active="False",
                true_perms=("perm_read",) + tuple(_DIRECTIONS))],
            data_noupdate="0"))
        self.assertEqual(fixture.directions(), [], "還有活的 manage rule，應該綠")
        fixture.write(fixture.rel, data_file(
            [member_read_rule(), rule_xml(
                "workspace_access_rule_dead", active="False",
                true_perms=("perm_read",) + tuple(_DIRECTIONS))],
            data_noupdate="0"))
        got = fixture.directions()
        self.assertEqual(self._directions_named(got), sorted(_DIRECTIONS),
                         "唯一涵蓋三方向的 rule 是 active=False 的，該紅：%s" % blob(got))

    def test_an_omitted_perm_field_counts_as_true(self):
        """Odoo `ir.rule.perm_*` 是 `default=True` ⇒ 沒宣告等於**有**套用。

        把「沒寫」當 False 會讓一條合法省略 perm_write 的 rule 被判成沒守 write
        ——判定與 Odoo 實際行為相反（假陽性），而修法會是去加一堆 perm 欄位。
        """
        fixture = Fixture(self, data_file(
            [rule_xml("workspace_access_rule_defaults", omit_perms=True)],
            data_noupdate="0"))
        self.assertEqual(fixture.directions(), [])

    def test_model_binding_via_eval_ref_is_recognised(self):
        """特異性：`eval="ref('…')"` 也是合法的 model 綁定形態。"""
        fixture = Fixture(self, data_file(
            [rule_xml("workspace_access_rule_eval_form", model_ref_via_eval=True,
                      true_perms=("perm_read",) + tuple(_DIRECTIONS))],
            data_noupdate="0"))
        self.assertEqual(fixture.directions(), [],
                         "eval=\"ref('…')\" 形態沒被認出來 ⇒ 判定會變成「這條 rule "
                         "不綁任何 model」，整條守線靜默失效")

    def test_rules_on_another_model_do_not_count(self):
        """陰性對照：別的 model 的 manage rule 不能讓 workspace_access 轉綠。"""
        fixture = Fixture(self, data_file(
            [rule_xml("cloud_service_rule_manage",
                      model_ref="model_woow_paas_platform_cloud_service",
                      true_perms=("perm_read",) + tuple(_DIRECTIONS))],
            data_noupdate="0"))
        got = fixture.directions()
        self.assertEqual(self._directions_named(got), sorted(_DIRECTIONS), blob(got))


# ------------------------------------------------------------ ③ 訊息不得寫死會過期的值

class TestWorkspaceModelDirectionAnchor(unittest.TestCase):
    """④ `check_workspace_rule_directions` 的**錨點**——唯一 ③ 涵蓋不到的那一格。

    ④ 與 ③ 共用 `_rule_direction_violations`，判準那一半由 ③ 的測試守著。④ 自己
    獨有的只有一個輸入：`WORKSPACE_MODEL_REF`（要看 workspace **本體**，不是
    workspace_access）。把它指錯 ⇒ selector 永不命中 ⇒ ④ 永遠回 `[]`、`--all` 照樣
    把它列進通過清單 ＝ 一條看起來在守、實際沒在跑的 no-op gate。

    🔴 ③ 的測試**結構上不可能**抓到這個：它們注入的全是 `workspace_access` 的
    record，④ 指錯 model 時那些測試一條都不會變色。所以這裡只需要**一支**測試，
    但它必須錨在那個區別點上——注入 workspace **本體**那條 manage rule 的缺陷，
    斷言 ④ 轉紅**而且訊息點名 workspace 本體的 model**。

    ⚠ 這支也是「就地突變再跑」的形態 —— 見檔頭 `.pyc` 那一段的配方
    （本檔的注入都在 tempfile 假 root 上做，不動真檔案，所以不受那個陷阱影響；
    受影響的是**改 `repo-lint.py` 自己**的那種突變）。
    """

    MANAGE_RULE = "workspace_rule_owner_admin_manage"

    def test_flipping_the_workspace_manage_rule_reds_gate4_and_names_the_workspace_model(self):
        fixture = Fixture.from_real(self)
        self.assertEqual(rl.check_workspace_rule_directions(fixture.root), [],
                         "注入前就紅了——基準壞掉")
        clean = fixture.text()

        # 注入：把 workspace 本體 manage rule 的 perm_write 翻掉（注入工具自帶絆線，
        # 錨點沒命中會 raise InjectionFailed 而不是靜默回傳未突變的原始碼）。
        fixture.flip_perm(self.MANAGE_RULE, "perm_write")

        got = rl.check_workspace_rule_directions(fixture.root)
        # 🔴 下面這兩行是「錨點漂到**第三個** model」那一格的唯一守衛（gate 層攔不
        #    住，見 repo-lint.py `_rule_direction_anchor_violations` 的「已知殘留」
        #    段）。它們有效的理由是上面那個注入錨在**寫死的 MANAGE_RULE xmlid**
        #    ——錨點一漂，翻 workspace manage rule 的 perm_write 就打空 ⇒ got 縮成
        #    [] ⇒ 第一行紅；漂到一個剛好也缺方向的 model 時 len(got) 仍可能是 1，
        #    那時由第二行（訊息點的方向對不上）接手。
        #    ⇒ 不要「簡化」掉這兩行，也不要把 MANAGE_RULE 改成從 WORKSPACE_MODEL_REF
        #    推導（注入會跟著錨點一起漂，這一格就沒有守衛了）。
        self.assertEqual(len(got), 1, "④ 應該恰好報一條（perm_write）：%s" % blob(got))
        self.assertIn("perm_write", got[0], "訊息沒點名是哪個方向")
        # ⚠ 下面這行是**自我指涉**的：違規訊息本來就把 `model_ref` 內插進去，而這裡
        #    讀的就是同一個常數 ⇒ 只要 got 非空它就恆綠，對錨點漂移零鑑別力。**不是
        #    它在守這一格**（#675 覆核 §1.1）。真的把「錨點沒指到 ③ 那個 model」釘住
        #    的是再下一行的 assertNotIn——它讀的是**另一個**常數，所以會紅。
        self.assertIn(rl.WORKSPACE_MODEL_REF, got[0],
                      "訊息沒點名 workspace **本體** 的 model——④ 的錨點可能指錯了")
        self.assertNotIn(rl.WORKSPACE_ACCESS_MODEL_REF, got[0],
                         "④ 報的是 workspace_access ⇒ 錨點指到 ③ 的 model 了")

        # 特異性：同一個注入不得讓 ③ 變色（證明兩條 gate 的錨點真的分開）。
        self.assertEqual(
            rl.check_workspace_access_rule_directions(fixture.root), [],
            "動 workspace 本體的 rule 卻讓 ③ 也紅了 ⇒ 兩條 gate 的 model 錨點沒有分開")

        fixture.write(fixture.rel, clean)
        self.assertEqual(rl.check_workspace_rule_directions(fixture.root), [],
                         "還原後仍紅——紅的原因不是注入，是 fixture／環境")

    # ------------------------------------------------------------------ #
    #  #675 覆核 F1：錨點被**複製貼上**成姊妹 check 的值時的靜默退化          #
    # ------------------------------------------------------------------ #
    #  上面那一支守的是「錨點指到一個沒有 rule 的地方」（打錯字形態）。它對
    #  **複製貼上**形態零鑑別力：`WORKSPACE_MODEL_REF` 被改成
    #  `WORKSPACE_ACCESS_MODEL_REF` 的值之後，④ 看的是 ③ 那組真的存在、真的有
    #  rule 的 record ⇒ 三個方向全被涵蓋、上面那支的注入（翻 workspace 本體 manage
    #  rule 的 perm_write）對它一點影響都沒有 ⇒ **綠**。覆核者實測到底：再把
    #  workspace 本體四條 rule 全刪掉，`--all` 仍然 EXIT=0。
    #  ⇒ 打錯字會紅、複製貼上不會，而相鄰宣告的兩個常數＋共用實作正是誘發複製貼上
    #  的形態（#612／#613 的「兩層判準退化成一層」同型）。

    def _workspace_rule_xmlids(self, root):
        """真樹上綁在 workspace **本體**的 ir.rule xmlid（從真相源推導，不寫死名字）。

        寫死那四個名字會讓這幾支在「規則正當改名」的當天紅在錯的地方（本檔已經被
        `18.0.1.0.122` 那種寫死版號咬過一次，見 `retirement_migration`）。
        """
        records, _ = rl.module_record_declarations(root)
        return sorted({r["xmlid"] for r in records
                       if r["model"] == rl.IR_RULE_MODEL
                       and rl._rule_model_ref(r["fields"]) == rl.WORKSPACE_MODEL_REF})

    def test_the_anchor_patch_actually_reaches_the_gate(self):
        """絆線：`anchor_temporarily` 這種改法必須真的影響判定。

        下面兩支都靠「改模組層常數 → gate 轉紅」來驗。如果 gate 哪天把錨點在 import
        當下就綁進 closure／預設參數，setattr 就再也影響不到它 ⇒ 那兩支會靜默退化成
        「注入根本沒發生」，而輸出與「gate 正確」完全相同。所以先用一個**不需要
        F1 修法也一定會紅**的錨點（指到一個不存在的 model）證明這條路是通的。
        """
        fixture = Fixture.from_real(self)
        self.assertEqual(rl.check_workspace_rule_directions(fixture.root), [],
                         "注入前就紅了——基準壞掉")
        anchor_temporarily(self, WORKSPACE_MODEL_REF="model_no_such_model_at_all")
        got = rl.check_workspace_rule_directions(fixture.root)
        self.assertTrue(got, "改掉模組層錨點常數之後 gate 沒有任何反應 ⇒ 它不是在呼叫"
                             "當下讀這個常數，下面兩支的注入手法已經失效")
        self.assertTrue(
            any("model_no_such_model_at_all" in v for v in got),
            "紅了但訊息裡沒有注入的值 ⇒ 紅的原因可能不是這次注入：%s" % blob(got))

    def test_an_anchor_pointing_at_a_model_with_no_declared_rule_says_the_anchor_is_broken(self):
        """錨點對不到任何**宣告出來的** ir.rule ⇒ 必須說「錨點壞了」。

        沒有這一層，同一個症狀只會產生三條「這個方向沒人守」——而那句話會把讀的人
        導向「去補 rule」，實際該做的是改常數。兩種病因的修法相反，訊息必須分得開。
        """
        fixture = Fixture.from_real(self)
        anchor_temporarily(self, WORKSPACE_MODEL_REF="model_woow_paas_platform_workspaces_typo")
        got = rl.check_workspace_rule_directions(fixture.root)
        self.assertTrue(
            any("錨點" in v and "對不到任何" in v for v in got),
            "沒有任何一條違規說是錨點壞了：%s" % blob(got))

    def test_copy_pasting_the_sibling_anchor_over_this_one_is_red_from_both_gates(self):
        """🔴 #675 覆核 F1 的直接回歸：兩個錨點相同 ⇒ ③ 與 ④ **兩條都要紅**。

        兩條 check 互相把對方的常數當 `sibling_ref` 傳進去，所以不論被複製貼上的是
        哪一個常數，兩條都會報。單向傳的話只有一條會紅，而報紅的會是「被抄走的
        受害者」那一條——訊息指向錯的地方。
        """
        fixture = Fixture.from_real(self)
        self.assertEqual(rl.check_workspace_rule_directions(fixture.root), [],
                         "注入前就紅了——基準壞掉")
        self.assertEqual(rl.check_workspace_access_rule_directions(fixture.root), [],
                         "注入前就紅了——基準壞掉")

        anchor_temporarily(self, WORKSPACE_MODEL_REF=rl.WORKSPACE_ACCESS_MODEL_REF)

        for name, got in (
                ("④ workspace 本體", rl.check_workspace_rule_directions(fixture.root)),
                ("③ workspace_access", rl.check_workspace_access_rule_directions(fixture.root))):
            with self.subTest(gate=name):
                self.assertTrue(
                    any("同一個值" in v for v in got),
                    "%s 沒有報「兩個錨點是同一個值」——複製貼上形態靜默通過了：%s"
                    % (name, blob(got)))

    def test_the_copy_pasted_anchor_stays_red_even_with_every_workspace_rule_deleted(self):
        """🔴 覆核者實測到底的那一格：M5 ＋ 四條 rule 全刪，原本 `--all` 仍 EXIT=0。

        這一支把那個組合釘死成紅。它與上一支不是重複：上一支證明「複製貼上會紅」，
        這一支證明「複製貼上**掩蓋不了**真正的缺口」——後者才是 F1 的實際後果
        （四條授權規則整組消失卻沒有任何訊號）。
        """
        fixture = Fixture.from_real(self)
        names = self._workspace_rule_xmlids(fixture.root)
        self.assertTrue(
            names,
            "真樹上綁在 %s 的 ir.rule 一條都沒有——前提不成立，下面的「刪掉」是空操作"
            % rl.WORKSPACE_MODEL_REF)
        for xmlid in names:
            fixture.drop_record(xmlid)          # 自帶絆線：沒命中會 raise
        self.assertEqual(
            self._workspace_rule_xmlids(fixture.root), [],
            "四條 rule 沒有真的被刪光——注入失效")

        anchor_temporarily(self, WORKSPACE_MODEL_REF=rl.WORKSPACE_ACCESS_MODEL_REF)
        got = rl.check_workspace_rule_directions(fixture.root)
        self.assertTrue(
            got,
            "workspace 本體的 ir.rule 已經一條不剩，④ 卻回零違規——它正在看 ③ 那組 "
            "record（#675 覆核 F1 實測的 EXIT=0 形態又回來了）")

    def test_the_two_direction_gates_look_at_disjoint_non_empty_rule_sets(self):
        """真樹上的存在性：兩個錨點互異，而且各自真的有 ir.rule 在身上。

        ⚠ **這一支不守「錨點漂到第三個 model」那一格**，別照名字讀成它在守：它只是
        拿兩個常數**當下的值**去真樹上取集合，所以錨點漂到另一個「也有 rule 的」
        model 時，互異仍成立、兩邊仍非空、仍不相交 ⇒ 三條斷言全綠。守那一格
        的是上面那支注入測試的 `assertEqual(len(got), 1)` ＋
        `assertIn("perm_write", got[0])`，理由寫在那裡的註解與 `repo-lint.py`
        `_rule_direction_anchor_violations` 的「已知殘留」段（#675 覆核 §1.1）。

        逐條寫清楚這裡各自證了什麼，免得下一個人以為三條都在出力：

          * `assertNotEqual`（互異）——與 gate 第一層同判準，這裡是測試層的 floor。
          * `assertTrue(names)`（各自有靶）——與 gate 第二層同判準，同上。只有注入
            測試的話，一份把錨點指到「沒有任何 rule 的 model」的樹仍可能全綠，因為
            每支注入只證明「某個特定的改壞會紅」。
          * `assertEqual(A & B, set())`（不相交）——**是前一條的定理，不是獨立檢查**：
            `rl._rule_model_ref()` 對每一筆 record 只回**一個**值，所以只要兩個錨點
            互異，兩個集合就必然不相交；`assertNotEqual` 先跑，它綠了這行不可能紅。
            留著當那條不變式的自我說明，不要把它算進涵蓋率。

        判準寫在**不變式層**而不是形態層（不點名那四個 xmlid）：規則正當改名的當天，
        形態層的版本會紅在完全錯誤的地方。
        """
        root = str(_REPO_ROOT)
        records, errors = rl.module_record_declarations(root)
        self.assertEqual(errors, [], "有 XML parse 不了，下面的集合都不可信")
        by_anchor = {}
        for ref in (rl.WORKSPACE_MODEL_REF, rl.WORKSPACE_ACCESS_MODEL_REF):
            by_anchor[ref] = {r["xmlid"] for r in records
                              if r["model"] == rl.IR_RULE_MODEL
                              and rl._rule_model_ref(r["fields"]) == ref}

        self.assertNotEqual(
            rl.WORKSPACE_MODEL_REF, rl.WORKSPACE_ACCESS_MODEL_REF,
            "兩條 direction gate 的 model 錨點是同一個值 ⇒ 第 25 條退化成第 23 條的"
            "複本（#675 覆核 F1）")
        for ref, names in sorted(by_anchor.items()):
            with self.subTest(model_ref=ref):
                self.assertTrue(
                    names,
                    "`%s` 在真樹上對不到任何一條宣告出來的 ir.rule——這條 gate 的"
                    "負向判準在空集合上恆真，它現在是 no-op" % ref)
        self.assertEqual(
            by_anchor[rl.WORKSPACE_MODEL_REF] & by_anchor[rl.WORKSPACE_ACCESS_MODEL_REF],
            set(),
            "兩條 gate 看的是同一批 record ⇒ 它們不是兩層判準，是同一層的兩個名字")


class TestViolationMessageDoesNotAssertStaleFacts(unittest.TestCase):
    """訊息裡的 ACL 值必須是**讀出來的**，不是寫死的（#659 驗收 F3）。

    原本訊息逐字寫「ACL 給 base.group_user 的是 1,1,1,1」。那是**今天**才成立的值
    （#556 修復前是 `1,1,1,0`），而這條 check 根本不讀 ACL ⇒ 同一條訊息印在舊樹上就是
    假話。本 repo 反覆被「宣稱過期成假話」咬過，所以這裡把「訊息反映實況」釘成契約。

    pass/fail 判準仍**與 ACL 脫鉤**（縱深防禦），下面第三支就是釘這件事。
    """

    def _red_fixture(self, **acl):
        fixture = Fixture(self, data_file([member_read_rule()], data_noupdate="0"))
        if acl:
            fixture.write_acl(**acl)
        return fixture

    def test_the_message_reflects_the_acl_file_it_reads(self):
        got = self._red_fixture(read="1", write="1", create="1", unlink="0").directions()
        self.assertTrue(got, "fixture 只有 read rule，三個寫入方向該紅")
        self.assertIn("1,1,1,0", blob(got),
                      "訊息沒有反映 fixture 裡的 ACL 實值 ⇒ 它印的是寫死的常數")
        self.assertNotIn("1,1,1,1", blob(got),
                         "訊息印出了 fixture 裡不存在的 ACL 值——寫死的斷言又回來了")

    def test_a_missing_acl_file_only_drops_the_clause(self):
        """讀不到 ACL 只少一句話，不得讓一條安全守線變成「因為 csv 不見而紅／而綠」。"""
        got = self._red_fixture().directions()          # 沒有寫 ACL csv
        self.assertEqual(len(got), len(_DIRECTIONS),
                         "違規條數不該受 ACL csv 有無影響：%s" % blob(got))
        self.assertNotIn("ACL 是", blob(got))
        self.assertNotIn("1,1,1", blob(got))

    def test_pass_fail_stays_decoupled_from_the_acl(self):
        """ACL 全關（0,0,0,0）時方向缺失照樣紅——判準與 ACL 脫鉤。

        縱深防禦：ACL 日後被放寬時規則層必須已經在，而「ACL 現在擋著」沒有任何守線盯著。
        """
        got = self._red_fixture(read="0", write="0", create="0", unlink="0").directions()
        self.assertEqual(len(got), len(_DIRECTIONS),
                         "ACL 全關就不報了 ⇒ 判準被 ACL 綁住了，縱深防禦沒了：%s"
                         % blob(got))
        self.assertIn("0,0,0,0", blob(got))


# ------------------------------------------------------------ fail-loud

class TestFailLoud(unittest.TestCase):
    """探針壞掉必須紅。負向判準在空集合上恆真 ⇒ 沉默＝永久假綠。"""

    def test_no_ir_rule_records_at_all_is_a_violation_for_every_check(self):
        # grandfathered=False：這支要驗的就是「一條 ir.rule 都沒有」，帶上 grandfather
        # 區塊會讓前提不成立（那兩條也是 ir.rule）。
        fixture = Fixture(self, data_file(
            [rule_xml("some_menu_record", model="ir.ui.menu", model_ref=None)],
            data_noupdate="0", grandfathered=False))
        for name, got in (("noupdate", fixture.noupdate()),
                          ("retired", fixture.retired()),
                          ("directions", fixture.directions())):
            with self.subTest(check=name):
                self.assertTrue(any("掃不到任何" in v for v in got),
                                "一條 ir.rule 都抽不到卻沒紅（%s）——selector 壞掉時"
                                "「沒有違規」與「從未命中」輸出一模一樣：%s"
                                % (name, blob(got)))

    def test_unparseable_xml_is_a_violation(self):
        fixture = Fixture(self, "<odoo><data noupdate='1'><record id='x'</odoo>")
        for name, got in (("noupdate", fixture.noupdate()),
                          ("retired", fixture.retired()),
                          ("directions", fixture.directions())):
            with self.subTest(check=name):
                self.assertTrue(any("parse 失敗" in v for v in got),
                                "parse 失敗被當成無事（%s）：%s" % (name, blob(got)))

    def test_a_missing_file_is_a_violation(self):
        """整份 XML 不見了（改名／搬走）也必須紅，不能靜默通過。"""
        fixture = Fixture(self, text=None)
        self.assertTrue(any("掃不到任何" in v for v in fixture.noupdate()))
        self.assertTrue(any("掃不到任何" in v for v in fixture.directions()))

    def test_records_moved_to_another_data_file_are_still_seen(self):
        """selector 綁死單一檔名的話，「搬到別的 data 檔」會讓守線靜默解除。"""
        fixture = Fixture(self, data_file(
            [rule_xml("moved_rule")], data_noupdate="1"),
            rel="src/data/extra_rules.xml")
        got = fixture.noupdate()
        self.assertTrue(any("moved_rule" in v for v in got),
                        "搬到 src/data/ 的 ir.rule 沒被看到：%s" % blob(got))
        self.assertIn("src/data/extra_rules.xml", blob(got),
                      "違規訊息沒指出是哪個檔")


# ------------------------------------------------------------ 分派 scope 一致性

class TestScanScopeAndHookScopeAgree(unittest.TestCase):
    """CI 掃的檔與 hook 接得到的檔必須是同一組——分岔就是「CI 會抓、hook 沉默」。"""

    def test_every_scanned_file_is_watched(self):
        scanned = [rel for rel, _ in rl._security_rule_xml_files(str(_REPO_ROOT))]
        self.assertGreaterEqual(len(scanned), 10, "掃到的檔太少——探針沒走到 repo")
        unwatched = [rel for rel in scanned if not rl._security_rule_watches(rel)]
        self.assertEqual(unwatched, [],
                         "這些檔 CI 掃得到、hook 卻接不到（分派條件與掃描範圍分岔）")

    def test_static_xml_and_non_xml_are_out_of_scope(self):
        for rel in ("src/static/src/paas/components/foo.xml",
                    "src/models/workspace.py",
                    "docs/decisions/patterns/README.md",
                    "extra/paas-operator/src/api/releases.py"):
            with self.subTest(rel=rel):
                self.assertFalse(rl._security_rule_watches(rel))
        scanned = [rel for rel, _ in rl._security_rule_xml_files(str(_REPO_ROOT))]
        self.assertFalse([rel for rel in scanned if rel.startswith("src/static/")],
                         "OWL template 被掃進來了——那裡沒有 record，只是白花時間")

    def test_the_real_security_file_is_in_scope(self):
        """肯定性：主要 carrier 真的在射程內（上面兩支都是否定性斷言）。"""
        scanned = [rel for rel, _ in rl._security_rule_xml_files(str(_REPO_ROOT))]
        self.assertIn(_REAL_RULES_REL, scanned)
        self.assertTrue(rl._security_rule_watches(_REAL_RULES_REL))


# ------------------------------------------------------------ 注入工具自身

class TestInjectionToolTripwires(unittest.TestCase):
    """🔴 注入工具的絆線本身要有測試——這是整份測試最容易做假的一環。

    絆線壞掉時的症狀是「注入靜默沒發生」，而那與「gate 正確」在輸出上完全相同。
    """

    def test_dropping_an_unknown_record_raises(self):
        with self.assertRaises(InjectionFailed):
            Fixture.from_real(self).drop_record("no_such_rule_xmlid")

    def test_flipping_a_perm_that_is_not_true_raises(self):
        with self.assertRaises(InjectionFailed):
            # member_read 的 perm_write 本來就是 False，錨點不該命中。
            Fixture.from_real(self).flip_perm(
                "workspace_access_rule_member_read", "perm_write")

    def test_inserting_into_a_missing_noupdate_block_raises(self):
        # grandfathered=False：預設 fixture 會帶一個 noupdate="1" 區塊，這支要驗的是
        # 「檔案裡根本沒有那個區塊」時絆線會 raise 而不是靜默不插入。
        fixture = Fixture(self, data_file([tenant_manage_rule()], data_noupdate="0",
                                          grandfathered=False))
        with self.assertRaises(InjectionFailed):
            fixture.insert_into_noupdate_block(rule_xml("x"))

    def test_appending_a_noupdate_block_without_the_closing_root_raises(self):
        """`append_noupdate_block` 的絆線也要被測到。

        新的注入工具與舊的一樣，錨點失效時必須 **raise** 而不是靜默回傳一份未突變的
        原始碼——那會讓 F3 那支把「東西根本沒注入」讀成「gate 沒抓到」（兩者的輸出
        都是 `0 != 1`）。注入工具本身沒有絆線的話，絆線哲學只寫在 docstring 裡。
        """
        fixture = Fixture(self, '<?xml version="1.0"?>\n<odoo>\n')
        with self.assertRaises(InjectionFailed):
            fixture.append_noupdate_block(rule_xml("x"))

    def test_flip_touches_only_the_target_record(self):
        """陽性對照：翻 perm 位只該動目標那一顆，不該掃到全檔的同名字串。"""
        fixture = Fixture.from_real(self)
        before, _ = rl.module_record_declarations(fixture.root)
        fixture.flip_perm("workspace_access_rule_owner_admin_manage", "perm_write")
        after, _ = rl.module_record_declarations(fixture.root)
        changed = []
        for old, new in zip(before, after):
            self.assertEqual(old["xmlid"], new["xmlid"], "record 順序變了——切片改到結構")
            if rl._rule_flag(old["fields"], "perm_write") != rl._rule_flag(new["fields"], "perm_write"):
                changed.append(new["xmlid"])
        self.assertEqual(changed, ["workspace_access_rule_owner_admin_manage"],
                         "被改到的 record 不只目標那一顆（或一顆都沒改到）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
