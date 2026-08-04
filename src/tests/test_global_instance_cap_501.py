# -*- coding: utf-8 -*-
"""#501：全域實例上限硬閘（A）＋ 停售開關脫離發版節奏（B）。

平台原本只有 `max_per_partner`（每帳號上限）。它保證「N 台散在 ≥N 個帳號、不會被
單一租戶掃光」，但**擋不住第 9、10、… 個不同帳號下單**——HAOS 首批限量 8 台的商業
前提因此沒有任何程式機制承載，超賣的失敗形態是租戶已付款卻卡在 Pending。

## 載體與缺值語意（本檔守的核心決策）

上限與停售都放 `ir.config_parameter`，key 由 slug 推導：

    woow_paas_platform.global_instance_cap.<slug>
    woow_paas_platform.sales_paused.<slug>

**不放 seed 欄位**：`src/data/cloud_app_templates.xml` 是 `noupdate=0`，檔尾的
`_heal_sticky_helm_value_specs` 每次 `-u` 還會主動把 `ir.model.data.noupdate` 清成
False，手改 DB 保不住值。「不寫進 seed XML 的 model 欄位」看似可行，但那是**靠遺漏
成立**——誰為了對稱補一行 `<field .../>`，每次 `-u` 就把上限清掉。config_parameter
在結構上不可能被 `-u` 碰到。

兩個缺值方向**刻意相反**，判準是「缺值造成的失效後果」不對稱：

* **上限缺值 ⇒ 走 code 內建預設**（fail-closed）。缺值若等於「不限」，就等於把
  「不超賣」這個不可逆的商業保證掛在「有人記得去設定」上。全新 DB／營運忘了設，
  硬閘照樣要在。
* **停售缺值 ⇒ 未停售**（fail-open）。停售缺值只造成「該手動收手卻沒收」，而超賣
  仍被上限硬閘擋住；反過來設會讓 `-u` 後或全新環境直接不可購買，而且沒有任何訊號
  告訴營運去打開它——守線的預設值不該是「商城關門」。

⚠️ Odoo 框架硬約束：`res.config.settings.set_values()` 對 Integer 是
`repr(value) if value else False`，而 `set_param(key, False)` 會 **unlink 該列**。
所以在設定畫面「填 0」與「從未設定」在儲存層是同一個狀態——「0＝不限」（
`max_per_partner` 的既有語意）在那條路上**物理上無法表達**。要表達「不限」只能經
Technical → Parameters 直接寫 `0`（本檔 `test_explicit_zero_param_means_unlimited`
釘住這條逃生門）。

## ⚠️ 守線邊界——看到本檔全綠**不代表併發已被守住**

本檔證明的是**判準與 enforcement 的正確性**，不是**並發下的互斥**。
`TransactionCase` / `HttpCase` 都在單一連線的單一交易內跑，兩個並發 create 在測試
裡構造不出來。

⚠️ 本檔原本在這裡寫著「READ COMMITTED 下取鎖後的 count 用新 snapshot」——那是從
#345 沿用的**錯誤前提**，已由 #518 證偽：Odoo 的 cursor 是 **REPEATABLE READ**
（`odoo/sql_db.py` 的 `set_isolation_level(ISOLATION_LEVEL_REPEATABLE_READ)`），
取鎖**不會**刷新 snapshot，單靠 advisory lock 的守衛在並發下零貢獻。

現行機制改為「寫衝突列 → 並發第二者拿 40001 → `retrying()` 以新 tx／新 snapshot
重試」。本檔能觀測的只有 `test_capped_create_writes_the_conflict_row`（衝突列真的
被寫）與 `test_unlimited_create_leaves_conflict_row_untouched`（無關商品不受牽連）。
**互斥與重試後的計數正確性由 PR 內的外部真並發腳本證明，不在本檔範圍**——這個缺陷
長期未被發現，正是因為既有測試只證「鎖被取得且鍵正確」、不證互斥後的計數正確性。

## 刻意不做的事

* **不硬編 8**。售罄與擴量都是正常業務動作，把今天的數字寫進斷言，下次調整會讓
  這個講「上限機制」的檔案集體變紅。HAOS 只斷言「內建上限 > 0」，行為矩陣一律用
  合成 fixture 的 N。
* **不寫「未達上限時不取鎖」這類 `assert_not_called` 式的快路徑斷言**。
  `test_template_max_per_partner_336.test_no_advisory_lock_when_unlimited` 是現成
  的反例：它把「不限時省略取鎖」這個實作捷徑寫成契約，日後若改成更安全的無條件
  取鎖，受害的是守線而不是使用者。自檢問句——這條測試變紅時是使用者受害，還是
  只是實作換了寫法？
"""
from odoo.exceptions import ValidationError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.woow_paas_platform.models.cloud_app_template import SupplyGateError

import json

MOD = 'woow_paas_platform'
CAP_KEY = MOD + '.global_instance_cap.%s'
PAUSE_KEY = MOD + '.sales_paused.%s'


def _ctrl():
    from odoo.addons.woow_paas_platform.controllers.paas import PaasController
    return PaasController()


class _CapFixtureMixin:
    """合成 template ＋ 每台服務一個獨立 partner 的工具。

    每台一個 partner 是**刻意**的：全域上限要擋的正是「散在不同帳號」的下單，
    共用 partner 的 fixture 會讓 `max_per_partner` 先擋掉，測到的就不是本題。
    """

    def _make_template(self, slug, **extra):
        vals = {
            'name': 'Cap Fixture %s' % slug,
            'slug': slug,
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/%s' % slug,
            # #552 層 4：空版號現在會讓 template 直接不可購買（reason
            # `unpinned_chart`），會**遮蔽**本檔要測的 paused／sold_out 判定。
            # 釘一個版本 ＝ 讓 fixture 回到「正常上架商品」的形狀，本檔的判準不變。
            'helm_chart_version': '1.0.0',
        }
        vals.update(extra)
        return self.env['%s.cloud_app_template' % MOD].create(vals)

    def _set_cap(self, template, value):
        self.env['ir.config_parameter'].sudo().set_param(
            CAP_KEY % template.slug, value)

    def _set_paused(self, template, value):
        self.env['ir.config_parameter'].sudo().set_param(
            PAUSE_KEY % template.slug, value)

    def _make_workspace(self, tag):
        """一個全新 partner／user／workspace，彼此不共用帳號。"""
        partner = self.env['res.partner'].create({'name': 'Cap %s' % tag})
        user = self.env['res.users'].create({
            'name': 'Cap %s' % tag,
            'login': 'cap-%s-501' % tag,
            'partner_id': partner.id,
        })
        return self.env['%s.workspace' % MOD].create({
            'name': 'ws-%s' % tag, 'owner_id': user.id})

    def _make_service(self, template, tag, **extra):
        vals = {
            'name': 'svc-%s' % tag,
            'workspace_id': self._make_workspace(tag).id,
            'template_id': template.id,
        }
        vals.update(extra)
        return self.env['%s.cloud_service' % MOD].create(vals)

    def _fill_to_cap(self, template, cap):
        """把 template 填滿到剛好 cap 台，每台各屬不同帳號。"""
        return [self._make_service(template, '%s-%d' % (template.slug, i))
                for i in range(cap)]

    def _assert_sales_paused_is_live(self, template, tag):
        """陽性對照：確認停售旗標**此刻真的生效**。

        「停售期間 X 仍寫得進去」這類斷言，若 `_set_paused` 失效（key 改名、param
        沒寫進去），會在「根本沒停售」的情況下同樣通過 ⇒ 整條退化成恆真而無人察覺。
        用 `create()` 反向確認：停售中的 template 不該再收得下新的實例。

        擺在**主斷言之後**呼叫——這裡的 create 會 raise，讓它離開主斷言的路徑。
        """
        with self.assertRaises(
                SupplyGateError,
                msg='停售旗標沒生效 ⇒ 本條測不到「停售期間」，鑑別力歸零'):
            self._make_service(template, tag)


@tagged('post_install', '-at_install')
class TestGlobalCapResolution(_CapFixtureMixin, TransactionCase):
    """`_global_instance_cap()` 的缺值／覆寫語意。"""

    def test_template_without_param_or_builtin_is_unlimited(self):
        """一般商品沒有 param 也不在內建表 ⇒ 0（不限）。

        這條是「加了守線不會讓整個商城關門」的保證：新欄位／新守衛的預設狀態
        必須是不干預。
        """
        tpl = self._make_template('cap-none-501')
        self.assertEqual(tpl._global_instance_cap(), 0)

    def test_ordinary_seed_template_is_unlimited(self):
        """實查 seed：非限量商品不得意外長出上限。"""
        n8n = self.env.ref('%s.cloud_app_n8n' % MOD)
        self.assertEqual(n8n._global_instance_cap(), 0)

    def test_haos_has_a_builtin_nonzero_cap(self):
        """🔴 HAOS 必須有內建上限——**不斷言等於幾**。

        數字是商業決策（首批 8 台，之後會調），寫進斷言等於把今天的供給量鎖成
        契約。這裡守的是不變式：限量商品在沒有任何 runtime 設定時仍有硬閘。
        """
        haos = self.env.ref('%s.cloud_app_haos' % MOD)
        self.assertGreater(
            haos._global_instance_cap(), 0,
            'HAOS 沒有內建全域上限 ⇒ 全新 DB／營運忘了設定時可無限超賣')

    def test_param_overrides_builtin_default(self):
        """B 案本體：不發版就能改上限。

        目標值由內建值推導而來，不寫死——這條測的是「param 蓋得過內建」，不是
        「上限是幾」。
        """
        haos = self.env.ref('%s.cloud_app_haos' % MOD)
        builtin = haos._global_instance_cap()
        self._set_cap(haos, str(builtin + 5))
        self.assertEqual(haos._global_instance_cap(), builtin + 5)

    def test_malformed_param_falls_back_to_builtin(self):
        """param 壞掉（手改成非數字）⇒ 回內建預設，不是「不限」。

        fail-open 在這裡的代價是超賣，而超賣不可逆。
        """
        haos = self.env.ref('%s.cloud_app_haos' % MOD)
        builtin = haos._global_instance_cap()
        self._set_cap(haos, 'eight')
        self.assertEqual(haos._global_instance_cap(), builtin)

    def test_negative_param_falls_back_to_builtin(self):
        """🔴 負數 ⇒ 回內建預設，**不是**「不限」。

        原實作是 `max(int(raw), 0)`，負數被夾成 0，而 0 在本設計＝不限 ⇒ 不取鎖、
        不計數、前台永不售罄，硬閘整個關掉且零訊號。

        可達情境不是「填 -1 想表達不限」（那意圖達成、無損害），而是**正負號打錯**
        ——要填 8 打成 -8。營運意圖是設限，畫面顯示 -8，實際全開。

        同一個 method 對非數字（`'eight'`）刻意 fail-closed 並發 warning；同一種
        營運手滑，兩個形態的處理方向必須一致。
        """
        haos = self.env.ref('%s.cloud_app_haos' % MOD)
        builtin = haos._global_instance_cap()
        self._set_cap(haos, '-8')
        self.assertEqual(
            haos._global_instance_cap(), builtin,
            '負數上限被當成「不限」⇒ 正負號打錯就把硬閘整個關掉')

    def test_explicit_zero_param_means_unlimited(self):
        """顯式寫 `0` ⇒ 不限（Technical → Parameters 的逃生門）。

        設定畫面填 0 會被 Odoo 存成「刪掉該列」＝缺值，所以「解除限量」在設定畫面
        沒有表達方式；這條釘住唯一有的那條路徑，避免日後有人把它「順手修掉」。
        """
        haos = self.env.ref('%s.cloud_app_haos' % MOD)
        self._set_cap(haos, '0')
        self.assertEqual(haos._global_instance_cap(), 0)


@tagged('post_install', '-at_install')
class TestSalesPausedResolution(_CapFixtureMixin, TransactionCase):
    """`_sales_paused()` 的缺值語意，以及與設定畫面同語意。"""

    def test_missing_param_is_not_paused(self):
        tpl = self._make_template('pause-none-501')
        self.assertFalse(tpl._sales_paused())

    def test_param_set_pauses(self):
        tpl = self._make_template('pause-set-501')
        self._set_paused(tpl, True)
        self.assertTrue(tpl._sales_paused())

    def test_clearing_the_param_resumes(self):
        """設定畫面取消勾選 ⇒ `set_param(False)` ⇒ 該列被 unlink ⇒ 回到未停售。

        這條把「Odoo 用刪列表達 False」這個框架行為釘進契約——否則有人改成讀
        `get_param(key) == 'False'` 之類的比對時不會有任何守線變紅。
        """
        tpl = self._make_template('pause-clear-501')
        self._set_paused(tpl, True)
        self._set_paused(tpl, False)
        self.assertFalse(tpl._sales_paused())


@tagged('post_install', '-at_install')
class TestGlobalCapEnforcement(_CapFixtureMixin, TransactionCase):
    """硬閘的行為矩陣。

    ⚠️ 這些測試在 `TransactionCase` 預設的 su env 下跑——守衛若用 `env.su` 或
    `has_group` 做豁免，deny 分支永遠走不到、整組會假綠（見 pattern doc
    `su-env-exempt-guard-false-green.md`）。它們現在會紅，正說明沒有豁免。
    """

    CAP = 2  # 合成值：大到能證明「未達上限放行」，小到跑得快。與任何商品無關。

    def setUp(self):
        super().setUp()
        self.tpl = self._make_template('cap-enforce-501')
        self._set_cap(self.tpl, str(self.CAP))

    def test_under_cap_is_allowed(self):
        services = self._fill_to_cap(self.tpl, self.CAP)
        self.assertEqual(len(services), self.CAP)

    def test_cap_blocks_across_different_partners(self):
        """🔴 #501 本體：第 CAP+1 台由**另一個帳號**下單，仍必須被擋。

        這正是 `max_per_partner` 擋不住的缺口——每台都屬不同 partner，每帳號上限
        永遠不會觸發。
        """
        self._fill_to_cap(self.tpl, self.CAP)
        with self.assertRaises(SupplyGateError):
            self._make_service(self.tpl, 'overflow')

    def test_suspended_and_error_still_occupy_a_slot(self):
        """停權／錯誤的實例照樣佔名額。

        口徑與 `_find_active_for_partner` 同源（`state not in TERMINAL_STATES`，
        而 TERMINAL_STATES 只有 `deleting`）。兩邊口徑漂移會讓同一台服務在兩個
        上限裡算法不同。
        """
        self._make_service(self.tpl, 'susp', state='suspended')
        self._make_service(self.tpl, 'err', state='error')
        with self.assertRaises(SupplyGateError):
            self._make_service(self.tpl, 'overflow')

    def test_deleting_frees_a_slot(self):
        """`deleting` 是唯一的釋放中終態 ⇒ 不佔名額。"""
        self._fill_to_cap(self.tpl, self.CAP)
        self.env['%s.cloud_service' % MOD].search([
            ('template_id', '=', self.tpl.id)], limit=1).state = 'deleting'
        self.assertTrue(self._make_service(self.tpl, 'reuse'))

    def test_paused_blocks_even_under_cap(self):
        """停售與售罄正交：還有名額也照樣不收單。"""
        self._set_paused(self.tpl, True)
        with self.assertRaises(SupplyGateError):
            self._make_service(self.tpl, 'paused')

    def test_unlimited_template_is_not_blocked(self):
        """非回歸：沒有上限的商品不受本守衛影響。"""
        plain = self._make_template('cap-plain-501')
        for i in range(self.CAP + 2):
            self._make_service(plain, 'plain-%d' % i)
        self.assertEqual(
            self.env['%s.cloud_service' % MOD].search_count(
                [('template_id', '=', plain.id)]), self.CAP + 2)

    def _row_version(self, template):
        """讀實體列版本 `ctid`——UPDATE 必定產生新 tuple ⇒ ctid 必變。

        刻意不看 `write_date`：那是**寫了什麼**，屬實作細節（改成寫別的欄位、或
        用交易級的 `now()`，不變式一樣成立）。要守的是「這一列真的被更新過」，
        因為並發衝突偵測靠的正是 tuple 版本，不是欄位內容。
        """
        self.env.cr.execute(
            "SELECT ctid FROM woow_paas_platform_cloud_app_template "
            "WHERE id = %s", (template.id,))
        return self.env.cr.fetchone()[0]

    def test_capped_create_writes_the_conflict_row(self):
        """🔴 #518：有上限的 create 必須寫「衝突列」（template row）。

        Odoo 的 cursor 是 **REPEATABLE READ**（`odoo/sql_db.py` 的
        `set_isolation_level(ISOLATION_LEVEL_REPEATABLE_READ)`），所以「取鎖之後
        重新計數」在同一個 tx 內**拿不到新 snapshot**——輸家取得鎖後看到的仍是
        舊資料，照樣放行。單靠 advisory lock 的守衛在並發下零貢獻。

        正解是讓並發的第二個 tx 撞上 write-write 衝突（PostgreSQL 在 RR 下回
        `could not serialize access due to concurrent update`，SQLSTATE 40001），
        由 Odoo 的 `retrying()` 以**新 tx／新 snapshot** 重試，計數才會正確。

        本條只證「衝突列真的被寫」——這是那個機制唯一可在單連線內觀測的部分。
        **互斥與計數正確性由外部真並發腳本證明**（見 PR 說明），不在本檔範圍。
        """
        before = self._row_version(self.tpl)
        self._make_service(self.tpl, 'conflict')
        self.assertNotEqual(
            self._row_version(self.tpl), before,
            '有上限的 create 沒有寫衝突列 ⇒ 並發第二者不會拿到 40001、'
            'retrying 不會重試、RR 的陳舊 snapshot 讓上限形同虛設')

    def test_unlimited_create_leaves_conflict_row_untouched(self):
        """沒有上限的商品不寫衝突列——否則全站每次建服務都在同一列上序列化。

        這條守的是**使用者可觀測的代價**（無關商品被拖慢），不是實作捷徑：
        改成無條件寫入時它會紅，而那個改動確實會傷到人。
        """
        plain = self._make_template('cap-noconflict-501')
        before = self._row_version(plain)
        self._make_service(plain, 'noconflict')
        self.assertEqual(self._row_version(plain), before)

    def test_write_is_deliberately_unguarded(self):
        """🔴 **這條測試守的是「刻意不守」**——`write()` 沒有上限守衛是設計決定。

        曾經有過一道（#501 B3，擋「先建成 `deleting` 再 write 回計數態」的繞道），
        移除的理由見 `cloud_service.write()` 的註解與 #519：它誤傷刪除失敗的善後
        路徑（`_delete_service` 在 except 寫 `state='error'`，正是「從終態寫回非終
        態」，而裁決點的 paused 檢查排在計數之前 ⇒ 營運一勾停售就讓所有刪除失敗
        rollback），而且擋不住 `template_id` 與 `state=False` 兩條繞道。

        ⚠ 本條**不是**在保護那個繞道、也不是把捷徑寫成契約。它是**行為文件**：
        誰要在 `write()` 補守衛，會先看到這裡的紅字與上面兩個理由，而不是以為
        「漏了」就順手補回去——那會把已修好的誤傷再帶回來。真正的修法是收緊 ACL
        （#519），不是在 write() 打地鼠。

        繞道本身**可達**（租戶對 cloud_service 有全 CRUD），這正是 #519 要解的。

        ⚠ **觸發器必須是「停售」而不是「填滿 cap」**，否則本條對它要偵測的 mutation
        零鑑別力——這正是被刪掉的 `test_write_between_counted_states_is_not_guarded`
        的死因（覆核 B-4），第一版的本條犯了同一個錯，實測才抓到：

        `_enforce_supply_gate` 的判定式是**嚴格大於**，且它預期「計數已含剛落庫的
        這筆」（create 路徑成立）。但 write 路徑的守衛在 `super().write()` **之前**
        求值，此時該筆還是 `deleting`、不被計數 ⇒ 計數停在 `cap`、`cap > cap` 為假
        ⇒ **守衛就算加回來也照樣放行**，本條照樣綠。（順帶說明那道守衛在 write 路徑
        本來就有 off-by-one，又一個它不該存在的理由。）

        `paused` 分支則在計數**之前**就 return，不受這個 off-by-one 影響 ⇒ 守衛一旦
        回歸必然 raise。實測：注入守衛原文 → 本條紅；移除 → 綠。
        """
        self._fill_to_cap(self.tpl, self.CAP)
        sneaked = self._make_service(self.tpl, 'sneak', state='deleting')
        # 停售要在 fixture 建好之後才開，否則 create() 的閘門會擋掉 fixture 本身。
        self._set_paused(self.tpl, True)

        sneaked.write({'state': 'pending'})
        self.assertEqual(
            sneaked.state, 'pending',
            'write() 出現了上限守衛。若這是刻意加的，請先確認它不會誤傷 '
            '_delete_service 的 except 分支（state deleting → error），並更新 #519。')
        self._assert_sales_paused_is_live(self.tpl, 'sneak-probe')

    def test_delete_failure_path_survives_sales_pause(self):
        """🔴 回歸守線：停售時「刪除失敗的善後」必須寫得進去（B3 誤傷的那個形態）。

        `_delete_service` 先寫 `state='deleting'`，uninstall 失敗後在 except 分支寫
        `state='error'` ＋ `error_message`。B3 那道守衛把這一步判成「進入計數態」，
        而裁決點 `_reject_reason_for_new_instance` 的 paused 檢查排在計數**之前**
        就 return ⇒ 停售期間任何刪除失敗都 raise、整筆 rollback、錯誤訊息遺失。

        鑑別力：把 B3 那段守衛加回 `write()`，這條會 raise SupplyGateError 而紅。
        本條不走 HTTP——受測的是 model 層的可寫性，不是端點行為。

        ⚠ **停售必須在 fixture 建好之後才打開，順序不可對調**。第一版寫成先
        `_set_paused(True)` 再 `_make_service(...)`，結果 `create()` 的閘門（那道
        **刻意保留**的守衛）把 fixture 自己擋掉，測試以 SupplyGateError 收場——
        error 的來源是 create、與本條要驗的 write 無關，卻長得像「守衛還在」。
        真實情境本來就是這個順序：服務先存在、營運後來才停售。
        """
        svc = self._make_service(self.tpl, 'delfail', state='deleting')
        self._set_paused(self.tpl, True)

        svc.write({'state': 'error', 'error_message': 'uninstall failed'})
        self.assertEqual(
            svc.state, 'error',
            '停售期間 deleting → error 被擋下 ⇒ 刪除失敗的善後寫不進去，'
            '服務停在 deleting、錯誤原因遺失（B-1 的誤傷形態復發）')
        self.assertEqual(svc.error_message, 'uninstall failed')
        self._assert_sales_paused_is_live(self.tpl, 'delfail-probe')


@tagged('post_install', '-at_install')
class TestAvailabilityPayload(_CapFixtureMixin, TransactionCase):
    """`_format_template()['availability']` 的三態。"""

    CAP = 1

    def _availability(self, template):
        return _ctrl()._format_template(template)['availability']

    def test_unlimited_template_is_purchasable(self):
        tpl = self._make_template('avail-plain-501')
        self.assertEqual(
            self._availability(tpl),
            {'purchasable': True, 'reason': 'available'})

    def test_cap_set_but_under_cap_is_purchasable(self):
        """🔴 正向覆蓋：**有上限但還有名額** ⇒ 仍可購買。

        既有的 `test_unlimited_template_is_purchasable` 走的是 cap==0 的短路分支，
        碰不到計數那條路。計數若多算一（例如口徑漂移把 `deleting` 也算進來、或
        `+1` 寫錯位置），商品會在還有名額時就顯示售罄——靜默少賣，而所有既有
        斷言仍綠。守線只守單向就是這樣被穿過去的。
        """
        tpl = self._make_template('avail-room-501')
        self._set_cap(tpl, '2')
        self._fill_to_cap(tpl, 1)          # 用掉 1／2，還有 1 個名額
        self.assertEqual(
            self._availability(tpl),
            {'purchasable': True, 'reason': 'available'})

    def test_at_cap_is_sold_out(self):
        tpl = self._make_template('avail-full-501')
        self._set_cap(tpl, str(self.CAP))
        self._fill_to_cap(tpl, self.CAP)
        self.assertEqual(
            self._availability(tpl),
            {'purchasable': False, 'reason': 'sold_out'})

    def test_paused_is_paused(self):
        tpl = self._make_template('avail-paused-501')
        self._set_paused(tpl, True)
        self.assertEqual(
            self._availability(tpl),
            {'purchasable': False, 'reason': 'paused'})

    def test_paused_wins_over_sold_out(self):
        """優先序：停售是明示的營運意志，售罄是推導出來的狀態。"""
        tpl = self._make_template('avail-both-501')
        self._set_cap(tpl, str(self.CAP))
        self._fill_to_cap(tpl, self.CAP)
        self._set_paused(tpl, True)
        self.assertEqual(self._availability(tpl)['reason'], 'paused')

    def test_availability_counts_across_all_tenants(self):
        """計數必須 sudo：ir.rule `cloud_service_rule_workspace_member` 讓非 sudo 的
        search 只看得到呼叫者是成員的 workspace。全域計數必然跨租戶，少算＝守衛靜默
        fail-open。

        ⚠ **必須以真實租戶身分求值**。原版直接在 `self.env` 下斷言，而
        `TransactionCase` 的 uid 是 SUPERUSER——ir.rule 對 superuser **不套用**，
        所以把 `_count_active_for_template` 的 `.sudo()` 拿掉，那個版本**照樣綠**：
        它證明的是「計數正確」，不是「計數有 sudo」。這裡改用一個對所有 fixture
        workspace 都不是成員的租戶身分求值，`.sudo()` 才成為通過的必要條件。
        """
        tpl = self._make_template('avail-crosstenant-501')
        self._set_cap(tpl, str(self.CAP))
        self._fill_to_cap(tpl, self.CAP)

        outsider = self.env['res.users'].create({
            'name': 'Outsider 501',
            'login': 'cap-outsider-501',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        # 陽性對照：這個身分確實被 ir.rule 擋住——若它看得到別人的服務，
        # 本條就退化成與原版一樣的恆真斷言。
        visible = self.env['%s.cloud_service' % MOD].with_user(outsider).search_count(
            [('template_id', '=', tpl.id)])
        self.assertEqual(
            visible, 0,
            '這個租戶身分看得到別的 workspace 的服務 ⇒ ir.rule 沒生效，'
            '本條對「計數是否 sudo」失去鑑別力（fixture 或 ACL 變了）')

        avail = tpl.with_user(outsider)._availability()
        self.assertFalse(
            avail['purchasable'],
            '以看不到那些服務的租戶身分求值時變成可購買 ⇒ 計數沒有 sudo、'
            '每個租戶各自算自己的數 ⇒ 全域上限形同虛設')


@tagged('post_install', '-at_install')
class TestAvailabilityMatchesEnforcement(_CapFixtureMixin, HttpCase):
    """跨端點單點裁決：payload 說不可買 ⟺ create 真的被拒。

    手法比照 `test_marketplace_price_label_478` 的
    `test_card_price_exists_in_the_plans_endpoint`——**實打兩個端點比對**，
    刻意不在測試裡重寫一份判斷（那是拿實作驗實作，兩邊一起錯仍全綠）。
    這條是防「前端判準與後端授權判準漂移」的機械落點（#460 家族）。
    """

    CAP = 1

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.tpl = self._make_template('avail-e2e-501')
        self.plan = self.env['sh.subscription.plan'].create({
            'name': 'Cap E2E 501', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'service_type': 'cloud_service',
            'sh_no_of_billing_cycle': -1,
        })
        self.env['product.template'].sudo().create({
            'name': 'Cap E2E 501 Subscription', 'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id, 'list_price': 100,
        })
        self.tpl.subscription_plan_ids = [(6, 0, [self.plan.id])]
        self.ws = self.env['%s.workspace' % MOD].create({
            'name': 'ws-e2e-501', 'owner_id': self.admin_user.id})

    def _rpc(self, url, params):
        resp = self.url_open(url, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'id': 501, 'params': params,
        }), headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        self.assertIn('result', parsed, 'resp: %s' % parsed)
        return parsed['result']

    def _listed(self):
        r = self._rpc('/api/cloud/templates', {})
        self.assertTrue(r.get('success'), r)
        return next((t for t in r['data'] if t['id'] == self.tpl.id), None)

    def _try_create(self):
        return self._rpc('/api/workspaces/%s/services' % self.ws.id, {
            'action': 'create', 'template_id': self.tpl.id,
            'name': 'svc-e2e-501', 'plan_id': self.plan.id,
        })

    def test_purchasable_payload_matches_create_outcome(self):
        """售罄前後各打一次兩個端點，兩邊必須同進退。"""
        item = self._listed()
        self.assertIsNotNone(item, '前提：合成 template 出現在 marketplace')
        self.assertTrue(
            item['availability']['purchasable'],
            '尚未設上限時應可購買（回 %r）' % item['availability'])

        self._set_cap(self.tpl, str(self.CAP))
        self._fill_to_cap(self.tpl, self.CAP)
        self.env.flush_all()

        item = self._listed()
        self.assertFalse(
            item['availability']['purchasable'],
            '已達上限但 payload 仍說可購買（回 %r）' % item['availability'])
        result = self._try_create()
        self.assertFalse(
            result.get('success'),
            'payload 說不可購買，create 端點卻放行了（回 %r）' % result)
        self.assertIn(
            self.tpl.name, result.get('error') or '',
            '應把 model 產生的供給閘門訊息轉出（含商品名），而不是通用錯誤'
            '（回 %r）。刻意斷言 template 名稱而非措辭片段：措辭是 UX 決定、'
            '會改；「訊息可行動」才是契約。' % result)

    def test_sold_out_template_stays_listed_and_not_hidden(self):
        """售罄＝顯示並標示，**不是**下架。

        誤用 `is_hidden` 會讓商品整個從 marketplace 消失、詳情頁與 plans 也讀不到
        （曝光層四處都判它）；誤用 `conceal_instances` 更糟——已付費租戶手上
        running 的服務會被一起鎖死（#464 實測形態）。兩個旗標都不該被本功能碰。
        """
        self._set_cap(self.tpl, str(self.CAP))
        self._fill_to_cap(self.tpl, self.CAP)
        self.env.flush_all()

        self.assertIsNotNone(
            self._listed(), '售罄商品必須仍列在 marketplace（顯示並標示售罄）')
        self.assertFalse(self.tpl.is_hidden, '售罄不得翻動曝光層旗標')
        self.assertFalse(self.tpl.conceal_instances, '售罄不得翻動實例層旗標')


@tagged('post_install', '-at_install')
class TestSettingsFieldTargetsGuardParam(TransactionCase):
    """設定畫面寫的 key 必須就是守衛讀的 key。

    這是本設計最容易無聲漂移的接縫：settings 欄位的 `config_parameter` 是一個
    **靜態字串**，守衛那側是由 slug 推導出來的。兩者對不上時營運會以為自己調了
    上限、實際上守衛讀的還是內建預設，而且沒有任何東西會報錯。
    """

    def _config_parameter(self, field_name):
        return self.env['res.config.settings']._fields[field_name].config_parameter

    def test_cap_field_matches_haos_guard_key(self):
        haos = self.env.ref('%s.cloud_app_haos' % MOD)
        self.assertEqual(
            self._config_parameter('woow_haos_global_instance_cap'),
            haos._global_cap_param_key())

    def test_pause_field_matches_haos_guard_key(self):
        haos = self.env.ref('%s.cloud_app_haos' % MOD)
        self.assertEqual(
            self._config_parameter('woow_haos_sales_paused'),
            haos._sales_paused_param_key())
