#!/usr/bin/env python3
"""#659 三條 ir.rule 守線的**注入測試**（紅燈優先，不是「跑一遍看乾不乾淨」）。

守的三條（實作見 `repo-lint.py` 檔頭第 22／23 條）：

  ① `check_security_rule_noupdate`            ir.rule 不得**出生**在 noupdate="1" 區塊
  ② `check_retired_rule_xmlid`                #556 退役的 xmlid 不得**復活**
  ③ `check_workspace_access_rule_directions`  workspace_access 的寫入方向不得**留空**

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
import shutil
import tempfile
import unittest

_GATE_PATH = pathlib.Path(__file__).with_name("repo-lint.py")
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_REAL_RULES_REL = "src/security/ir_rules.xml"
_REAL_RULES = _REPO_ROOT / _REAL_RULES_REL
_RETIREMENT_MIGRATION = _REPO_ROOT / "src/migrations/18.0.1.0.122/post-migrate.py"


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

    def insert_into_noupdate_block(self, record):
        """把一顆 record 插進**第一個 `<data noupdate="1">` 區塊**的結尾。"""
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

    def test_the_grandfather_path_is_exercised_by_reality(self):
        """真檔案裡確實有 noupdate 區塊的 ir.rule ⇒ ① 的放行分支被現實驅動過。

        沒有這一支的話，① 在真 repo 上的綠可能只是「noupdate 區塊裡什麼都沒有」，
        而 grandfather 清單是否真的被用到完全沒有訊號（清單恆綠 ≠ 清單有效）。
        """
        records, _ = rl.module_record_declarations(str(_REPO_ROOT))
        in_noupdate = {r["xmlid"] for r in records
                       if r["model"] == rl.IR_RULE_MODEL and r["noupdate"]}
        self.assertTrue(in_noupdate,
                        "真檔案的 noupdate 區塊裡一條 ir.rule 都沒有——① 的放行分支"
                        "從未被現實驅動（#658 修好了嗎？那就把 grandfather 清單清掉）")
        self.assertEqual(in_noupdate, set(rl.SECURITY_RULE_NOUPDATE_ALLOWED),
                         "noupdate 區塊的 ir.rule 與 grandfather 清單不相等——① 應該"
                         "已經在報違規了，這支的前提與 gate 的判定分岔了")

    def test_retired_names_still_live_in_code_and_are_not_flagged(self):
        """特異性的真實靶子：退役名字今天仍**合法**以文字形式存在。

        migration 的 `_RETIRED` tuple 是這兩個名字的權威載體（它就是靠名字去 DELETE
        的），永遠會留著。字串判準會把它當違規；② 判的是「有沒有 `<record id=…>`
        宣告」，所以必須綠。
        """
        text = _RETIREMENT_MIGRATION.read_text(encoding="utf-8")
        for xmlid in rl.RETIRED_RULE_XMLIDS:
            self.assertIn(xmlid, text,
                          "%s 不在 migration 裡了——本支測試失去靶子，請確認退役機制"
                          "是否被改動" % xmlid)
        self.assertEqual(rl.check_retired_rule_xmlid(str(_REPO_ROOT)), [])

    def test_a_faithful_copy_of_the_real_file_is_green(self):
        """基準 recipe：fixture 複本必須與真 repo 同判定（比對實驗要固定 recipe）。"""
        self.assertEqual(Fixture.from_real(self).all_violations(), [])


# ------------------------------------------------------------ ① noupdate 出生地

class TestNoupdateBlockInjection(unittest.TestCase):
    """敏感度①：ir.rule 落在 noupdate="1" 區塊必須紅且點名 xmlid。"""

    def test_new_rule_in_the_real_noupdate_block_is_red_then_green(self):
        fixture = Fixture.from_real(self)
        self.assertEqual(fixture.noupdate(), [], "注入前就紅了——基準壞掉")
        clean = fixture.text()
        fixture.insert_into_noupdate_block(
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
        """#658 的兩條必須放行，否則守線第一天就紅。"""
        fixture = Fixture(self, data_file([member_read_rule()], data_noupdate="0"))
        self.assertEqual(fixture.noupdate(), [])

    def test_an_allowlist_entry_with_no_matching_record_is_red(self):
        """反向：清單比它豁免的東西活得久 ⇒ 同名 record 日後會無聲繼承豁免。"""
        fixture = Fixture(self, data_file([tenant_manage_rule()], data_noupdate="0",
                                          grandfathered=False))
        got = fixture.noupdate()
        self.assertEqual(len(got), len(rl.SECURITY_RULE_NOUPDATE_ALLOWED),
                         "每個沒有對應 record 的清單項都該被點名一次：%s" % blob(got))
        for xmlid in rl.SECURITY_RULE_NOUPDATE_ALLOWED:
            self.assertIn(xmlid, blob(got))

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
        """🔴 特異性核心：現況就有註解提到那兩個名字，字串判準會誤報。"""
        note = ("    <!-- #556 退役了 %s 與 %s，見 migration 18.0.1.0.122 -->\n"
                % tuple(sorted(rl.RETIRED_RULE_XMLIDS)))
        fixture = Fixture(self, data_file([tenant_manage_rule()],
                                          data_noupdate="0", prologue=note))
        self.assertEqual(fixture.retired(), [],
                         "只在註解裡提到退役名字被當成違規——那會逼人刪掉史料")
        # 肯定性對照：同一份 fixture 加上真的 <record> 就必須紅（證明上面的綠不是
        # 因為整條 check 對這份 fixture 沒在跑）。
        retired = sorted(rl.RETIRED_RULE_XMLIDS)[0]
        fixture.write(fixture.rel, data_file(
            [rule_xml(retired), tenant_manage_rule()], data_noupdate="0", prologue=note))
        self.assertTrue(any(retired in v for v in fixture.retired()),
                        "加了真的 <record> 卻還是綠 ⇒ 上面那句綠沒有鑑別力")

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
