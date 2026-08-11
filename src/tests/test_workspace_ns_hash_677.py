# -*- coding: utf-8 -*-
"""#677：租戶不得經 `name` 決定自己的 K8s namespace。

## 洞（#658／PR #675 標為已知殘留缺口的那一條）

兩件事湊起來才是洞：

1. `Workspace._generate_slug()` 對 `[a-z0-9_-]` 字串是**恆等函式**，而 `name` 是租戶
   本來就該自由指定的欄位 ⇒ 租戶完全決定自己的 `slug`。
2. namespace／公開子網域是 `md5(slug)[:8]` ＝ **32 bit 截斷**。`UNIQUE(slug)` 只擋
   得住**字面相同**的 slug，擋不住兩個不同 slug 映到同一個 namespace。

⇒ 攻擊者離線搜一個 `name` 讓 `md5(slug)[:8]` 撞上受害者的 ws_hash（那個值逐字印在
受害者每一個公開子網域上，不必猜），建自己的 workspace（**不需要是任何 workspace 的
成員**），部署任一服務，Helm release 就落進受害者的 K8s namespace。

本檔用的碰撞對是 issue 本文給的、且已獨立複驗過的實值：
`md5('ws-83256')[:8] == md5('ws-137425')[:8] == '7d82ce00'`。**刻意寫死**——測試裡
跑生日碰撞搜尋會讓它變慢且不確定。

## 修法：workspace 的 namespace 身分改為 stored 欄位 ＋ DB 層 UNIQUE

`woow_paas_platform.workspace.ws_hash`（8 hex，伺服器產生的隨機值）＋
`UNIQUE(ws_hash)` SQL constraint。所有 namespace／subdomain 推導改吃這個 stored 值，
不再吃 `slug`。

🔴 **為什麼不能用「create 時 search 一下有沒有撞到」那種檢查**（本檔 N2 是它的
陽性對照）：`Workspace._generate_slug()` 既有的唯一性迴圈
`while self.search_count([('slug','=',slug)]) > 0` **沒有 `sudo()`** ⇒ 走呼叫者身分
⇒ 受 ir.rule 限制 ⇒ **看不到別的租戶的 workspace**。攻擊者正好就站在「看不見受害者」
這個位置上，所以任何以呼叫者身分做的碰撞檢查對他一律回 0、**靜默放行**。今天字面
碰撞擋得住是靠 DB 的 `UNIQUE(slug)`（它不看 ir.rule）拋 IntegrityError，不是靠那個
迴圈。⇒ 唯一站得住的守線在 DB 層：N2 量出「攻擊者看不見」，N3 量出「DB 仍然擋得下」。

隨機值（`secrets.token_hex(4)`）而不是 `md5(slug)`／`hmac(secret, id)` 的理由寫在
`workspace.py::_generate_ws_hash` 的 docstring，這裡不重複。

## 既有部署不漂移（本修法可行的前提）

`cloud_service.helm_namespace` / `subdomain` 都是**存起來的**欄位，推導只發生在建立
當下；migration `18.0.1.0.129/post-migrate.py` step 3 把既有列回填成
`md5(slug)[:8]` ＝**舊公式的逐字結果** ⇒ 既有 workspace 的 namespace／subdomain
byte-identical。N8 把這個契約釘住。

⚠ 例外路徑（每次呼叫重新推導、不是只在建立當下算一次）：
`smart_home._generate_subdomain()` / `_generate_tunnel_name()` 與
`security_access._generate_canonical_name()`。回填保證它們算出同一個值，而 N10 更
進一步斷言「再推導錨在 stored hash 上，slug 事後怎麼變都不影響」——比「再推導不會
發生」強，因為它不依賴呼叫點盤點的完整性。

## TDD 紀律（照抄 #556／#657／#658，一字不可省）

- **全部走真 HTTP RPC**（`/web/dataset/call_kw`）。禁 `TransactionCase`：它的 env
  `uid == SUPERUSER_ID` ⇒ `workspace.py` 的 `_is_privileged_writer()` 恆真 ⇒ 兩道
  守衛的 deny 分支結構上不可達，恆綠且與修復好壞無關。
- fixture user 一律只掛 `base.group_user`，且攻擊者**不是**受害者 workspace 的成員。
- **否定性斷言一律配陽性對照**：N1／N4-N6 斷言「被擋下」，N7 斷言「不碰撞的正常
  建立仍然成功」；N2 斷言「攻擊者真的看不見受害者」——沒有它，N1 綠了也不知道測試
  有沒有真的走在攻擊路徑上。

## 實測 RED（`fix/658-workspace-write-authz` @ `0cbe466d`，只加本測試檔）

原文輸出貼在 PR #675 的留言裡。紅的是 N1／N2／N3／N4／N5／N6／N7／N8／N9／N10
（10 條全紅）；其中 N1 與 N10 是**真碰撞／真漂移**形態的紅（斷言值 `7d82ce00` ==
`7d82ce00`、subdomain 前後不一致），其餘是欄位／守衛尚不存在的紅。兩種紅在報告裡
分開標，不要讀成「十條都證明了碰撞」。
"""
import json
import re
import uuid

from psycopg2 import IntegrityError

from odoo.tests.common import HttpCase, tagged
from odoo.tools import mute_logger

from odoo.addons.woow_paas_platform.services.naming import (
    generate_resource_hash,
    generate_ws_hash,
    make_namespace,
    make_security_access_subdomain,
    make_service_subdomain,
)

# issue 本文給的碰撞對，已獨立複驗（`hashlib.md5(s.encode()).hexdigest()[:8]`）。
VICTIM_NAME = 'ws-137425'
ATTACKER_NAME = 'ws-83256'
LEGACY_COLLIDING_HASH = '7d82ce00'

# `workspace.py` 兩道守衛的訊息特徵字串（同 #658 的做法：用它區分「model 層守衛
# 擋的」與「ir.rule 擋的」，否則歸因錯誤會全程隱形）。
GUARD_CREATE_WS_HASH_NEEDLE = 'Cannot supply a workspace ws_hash'
GUARD_WRITE_WS_HASH_NEEDLE = 'Cannot change the ws_hash of workspace'

# 從 `paas-{env}-sa-{ws_hash}-{res_hash}` 取出 ws_hash 段。刻意不寫成
# `split('-')[2]`：env token 存在與否會讓索引位移，索引寫死會在 stg 設定下靜默取錯段。
_SA_WS_SEGMENT_RE = re.compile(r'^paas-(?:[a-z0-9]+-)??sa-([0-9a-f]{8})-')


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 677' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class TestWorkspaceNamespaceHash677(HttpCase):
    """N 系列——租戶不得決定自己的 K8s namespace。

    fixture：`victim`（擁有 slug 為 `ws-137425` 的 workspace）與 `attacker`
    （**不屬任何 workspace**）。兩人只掛 `base.group_user`。
    """

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.victim, self.victim_login = _new_group_user(self.env, 'victim677')
        self.attacker, self.attacker_login = _new_group_user(self.env, 'attacker677')

        for u in (self.victim, self.attacker):
            self.assertFalse(
                u.has_group('base.group_system'),
                'fixture 前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        # 用 `with_user` 建（不是 superuser env）：這條路徑本身就是 create 守衛的
        # 陽性對照——fixture 建得起來就已經證明合法建立沒有被守衛打死。
        self.victim_ws = self.Workspace.with_user(self.victim).create(
            {'name': VICTIM_NAME})
        self.assertEqual(
            self.victim_ws.sudo().slug, VICTIM_NAME,
            'fixture 前提：`_generate_slug` 對這個字串是恆等函式（本 PR 沒有改它）')

    # ------------------------------------------------------------------ #
    #  helper                                                             #
    # ------------------------------------------------------------------ #
    def _call_kw(self, model, method, args, kwargs=None):
        """`/web/dataset/call_kw`——ORM 層攻擊必須走這條真實 RPC 路徑。"""
        body = json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'model': model, 'method': method,
                       'args': args, 'kwargs': kwargs or {}},
        })
        resp = self.url_open(
            '/web/dataset/call_kw', data=body,
            headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json()

    def _error_text(self, result):
        data = result.get('error', {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _assert_model_guard_denied(self, result, needle, msg):
        """斷言被 `workspace.py` 的 model 層守衛擋下（不是 ir.rule）。"""
        self.assertIn('error', result, msg + '（沒有被擋，回 %r）' % result)
        text = self._error_text(result)
        self.assertIn('AccessError', text,
                      msg + '（期望 AccessError，回 %r）' % result)
        self.assertIn(needle, text,
                      msg + '（期望是 model 層守衛擋的，回 %r）' % result)

    def _assert_ok(self, result, msg):
        self.assertNotIn('error', result, msg + '（回 %r）' % result)

    def _attacker_creates_colliding_workspace(self):
        """攻擊者以真 RPC 建一個 slug 與受害者**舊 hash 相撞**的 workspace。"""
        self.authenticate(self.attacker_login, self.attacker_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'create', [{'name': ATTACKER_NAME}])
        self._assert_ok(
            result, '建立本身是合法的（任何 group_user 都能建自己的 workspace），'
                    '本單修的是它落在哪個 namespace，不是擋掉建立')
        return self.Workspace.sudo().browse(result['result'])

    def _sa_canonical_name(self, workspace):
        """走 `security_access._generate_canonical_name()` 這條**生產推導路徑**。

        用 `.new()`（記憶體記錄，不寫 DB、不經 create 守衛）呼叫真正的生產方法，
        而不是在測試裡重寫一份公式——重寫的話，修好生產路徑而漏改測試的那天，
        測試量的是它自己那份影本，不是被部署的東西。
        """
        sa = self.env['woow_paas_platform.security_access'].sudo().new({
            'workspace_id': workspace.id,
            'name': 'ns-probe',
            'reference_id': 'ns-probe-ref',
        })
        return sa._generate_canonical_name()

    def _sa_ws_segment(self, canonical_name):
        m = _SA_WS_SEGMENT_RE.match(canonical_name)
        self.assertTrue(
            m, '無法從 %r 取出 ws_hash 段——命名格式變了，這條測試在量什麼要重新確認'
               % canonical_name)
        return m.group(1)

    def _smart_home_subdomain(self, workspace):
        """走 `smart_home._generate_subdomain()` 這條**每次呼叫都重新推導**的路徑。"""
        sm = self.env['woow_paas_platform.smart_home'].sudo().new({
            'workspace_id': workspace.id,
            'name': 'sm-probe',
            'reference_id': 'sm-probe-ref',
        })
        return sm._generate_subdomain()

    # ================================================================== #
    #  N1 —— 攻擊路徑本體                                                 #
    # ================================================================== #
    def test_N1_attacker_cannot_reach_the_victim_namespace_via_a_name_collision(self):
        """🔴 攻擊者用一個 md5 相撞的 `name` 建 workspace，公開命名不得落在受害者身上。

        這條是**真碰撞形態**的紅：修復前兩個 workspace 的 `paas-sa-{ws_hash}-…` 中段
        逐字相同（`7d82ce00`），斷言直接量到 `'7d82ce00' == '7d82ce00'`。

        走的是 `security_access._generate_canonical_name()` 這條生產推導路徑
        （見 `_sa_canonical_name` 的註解），不是測試自己重寫的公式。
        """
        # 前提（陽性對照）：舊推導對這兩個 slug 真的碰撞，而且撞在已知的那個值上。
        self.assertNotEqual(ATTACKER_NAME, VICTIM_NAME, '兩個 slug 必須字面不同')
        self.assertEqual(
            generate_ws_hash(ATTACKER_NAME), generate_ws_hash(VICTIM_NAME),
            '前提：舊推導 md5(slug)[:8] 是 32 bit 截斷，兩個不同 slug 映到同一值')
        self.assertEqual(
            generate_ws_hash(VICTIM_NAME), LEGACY_COLLIDING_HASH,
            '前提：碰撞值就是 issue 本文那一個（寫死，不在測試裡跑生日搜尋）')

        attacker_ws = self._attacker_creates_colliding_workspace()

        # 缺口的機制本身**沒有被關掉**，也刻意不關：slug 仍逐字等於租戶給的 name。
        self.assertEqual(
            attacker_ws.slug, ATTACKER_NAME,
            '本 PR 不改 slug 生成（那會波及 #525 A2 鎖死的行為）；改的是 namespace '
            '身分不再由 slug 決定')

        victim_seg = self._sa_ws_segment(self._sa_canonical_name(self.victim_ws))
        attacker_seg = self._sa_ws_segment(self._sa_canonical_name(attacker_ws))
        self.assertNotEqual(
            attacker_seg, victim_seg,
            '攻擊者的公開命名不得與受害者共用 ws_hash 段——共用即代表 Helm release '
            '會落進受害者的 K8s namespace')

    def test_N1b_the_two_workspaces_get_different_namespaces(self):
        """N1 的 namespace 版本（N1 量的是 subdomain 段，這條量 K8s namespace 本身）。"""
        attacker_ws = self._attacker_creates_colliding_workspace()
        self.assertNotEqual(
            attacker_ws.ws_hash, self.victim_ws.sudo().ws_hash,
            'ws_hash 是 namespace 身分，跨租戶必須唯一')
        self.assertNotEqual(
            make_namespace(attacker_ws.ws_hash),
            make_namespace(self.victim_ws.sudo().ws_hash),
            '兩個 workspace 不得映到同一個 K8s namespace')
        self.assertNotEqual(
            make_service_subdomain(attacker_ws.ws_hash, 'ref', 'svc'),
            make_service_subdomain(self.victim_ws.sudo().ws_hash, 'ref', 'svc'),
            'CloudService 子網域同樣不得共用 ws_hash 段')

    # ================================================================== #
    #  N2 —— 陽性對照：攻擊者結構上看不見受害者                            #
    # ================================================================== #
    def test_N2_attacker_structurally_cannot_see_the_victim_workspace(self):
        """🔴 **這條是選擇「DB 層 UNIQUE」而不是「create 時 search 檢查」的依據。**

        攻擊者以自己的身分 search 受害者的 workspace 一律回 0（ir.rule 讓他看不見），
        而 `sudo()` 看得到 1。⇒ 任何以呼叫者身分做的碰撞檢查在攻擊路徑上恆回「沒撞到」
        並**靜默放行**——這正是本 repo 記錄有案的「壞掉的探針最愛回答『零／空／
        找不到』」。能同時看見雙方的只有不看 ir.rule 的 DB constraint（N3）。

        沒有這條，N1 綠了也不知道測試有沒有真的走在攻擊路徑上（攻擊者若剛好看得見
        受害者，整組攻擊前提就不成立）。
        """
        victim_ws_id = self.victim_ws.id
        victim_hash = self.victim_ws.sudo().ws_hash

        self.authenticate(self.attacker_login, self.attacker_login)
        for domain, label in (
            ([('id', '=', victim_ws_id)], 'id'),
            ([('slug', '=', VICTIM_NAME)], 'slug'),
            ([('ws_hash', '=', victim_hash)], 'ws_hash'),
        ):
            result = self._call_kw(
                'woow_paas_platform.workspace', 'search_count', [domain])
            self._assert_ok(result, 'search_count(%s) 不該報錯' % label)
            self.assertEqual(
                result['result'], 0,
                '攻擊者以 %s 查受害者 workspace 必須回 0（他看不見）——回了非 0 '
                '代表本檔整組攻擊前提不成立' % label)

        self.assertEqual(
            self.Workspace.sudo().search_count([('ws_hash', '=', victim_hash)]), 1,
            '對照：同一筆資料 sudo 看得到 1。「DB 說有人用了」與「以攻擊者身分查說'
            '沒人用」同時成立，正是呼叫者身分的檢查擋不住跨租戶碰撞的機制')

    # ================================================================== #
    #  N3 —— DB 層 UNIQUE 是真正的後盾（繞過所有 Python 層）               #
    # ================================================================== #
    def test_N3_db_unique_constraint_rejects_a_duplicate_ws_hash_from_raw_sql(self):
        """🔴 直接下 SQL 把攻擊者的 `ws_hash` 改成受害者的值，DB 必須擋下。

        raw SQL 繞過 ORM、繞過 ir.rule、繞過 `write()` 守衛 ⇒ 這條量到的**只有**
        DB constraint 本身。它同時是「這個修法不怕 TOCTOU」的證明：保證來自寫入
        當下的唯一性約束，不是來自寫入之前的一次查詢。
        """
        attacker_ws = self._attacker_creates_colliding_workspace()
        victim_hash = self.victim_ws.sudo().ws_hash
        self.assertTrue(victim_hash, '前提：受害者必須已經有 ws_hash')
        self.env.flush_all()

        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    "UPDATE woow_paas_platform_workspace SET ws_hash = %s "
                    "WHERE id = %s", (victim_hash, attacker_ws.id))

        self.env.invalidate_all()
        self.assertNotEqual(
            attacker_ws.sudo().ws_hash, victim_hash,
            '被擋下之後值也不得落地')

    # ================================================================== #
    #  N4-N6 —— ws_hash 是 identity anchor（create 兩通道 ＋ write）        #
    # ================================================================== #
    def test_N4_tenant_cannot_supply_ws_hash_on_create(self):
        """🔴 租戶 create 時自帶 `ws_hash`——比 md5 preimage 搜尋**便宜得多**的同一條洞。

        沒有這道守衛的話，本單等於把攻擊從「搜 2^32 次 md5」降級成「填一個欄位」。
        """
        victim_hash = self.victim_ws.sudo().ws_hash
        self.authenticate(self.attacker_login, self.attacker_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'create',
            [{'name': 'ns squatter 677', 'ws_hash': victim_hash}])
        self._assert_model_guard_denied(
            result, GUARD_CREATE_WS_HASH_NEEDLE, 'create 不得自帶 ws_hash')
        self.assertFalse(
            self.Workspace.sudo().search([('name', '=', 'ns squatter 677')]),
            '記錄不得落地')

    def test_N5_tenant_cannot_inject_ws_hash_via_context_default(self):
        """🔴 `with_context(default_ws_hash=...)` ＋ vals **不放** ws_hash。

        #657 F2／#664 的同型：context 的 `default_<field>` 是 RPC 用戶端可控的
        （`call_kw` 是整份取代 context、不是 merge），而 default 要到
        `super().create()` 內部的 `_add_missing_default_values` 才補上。守衛看
        raw vals 就整道繞過。本 repo 已經在這個形態上踩過兩次，所以 create 守衛
        讀的是 `_add_missing_default_values()` 解析後的 `resolved`。
        """
        victim_hash = self.victim_ws.sudo().ws_hash
        self.authenticate(self.attacker_login, self.attacker_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'create',
            [{'name': 'ctx ns squatter 677'}],
            {'context': {'default_ws_hash': victim_hash}})
        self._assert_model_guard_denied(
            result, GUARD_CREATE_WS_HASH_NEEDLE,
            'context default_ws_hash 這條通道同樣要被擋')
        self.assertFalse(
            self.Workspace.sudo().search([('name', '=', 'ctx ns squatter 677')]),
            '記錄不得落地')

    def test_N6_owner_cannot_repoint_ws_hash_after_create(self):
        """🔴 workspace 的 **owner** 事後改 `ws_hash` 指向受害者——ir.rule 放行，
        只有 model 層 anchor 擋得住。

        用 owner（而不是 guest）是刻意的：owner 通過
        `workspace_rule_owner_admin_manage`，所以這條在「只收窄 ir.rule」的修法下
        完全不會變紅。同時它也守住既有部署——事後改 ws_hash 會讓已部署服務**存下來
        的** `helm_namespace` 與重新推導出來的值分岔。
        """
        attacker_ws = self._attacker_creates_colliding_workspace()
        victim_hash = self.victim_ws.sudo().ws_hash
        original = attacker_ws.ws_hash

        self.authenticate(self.attacker_login, self.attacker_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'write',
            [[attacker_ws.id], {'ws_hash': victim_hash}])
        self._assert_model_guard_denied(
            result, GUARD_WRITE_WS_HASH_NEEDLE, 'owner 不得事後改 ws_hash')
        self.assertEqual(
            attacker_ws.sudo().ws_hash, original,
            '除了回錯誤，值也必須沒落地（守衛必須在 super().write() 之前）')

    # ================================================================== #
    #  N7 —— 陽性對照：正常建立仍然成功                                    #
    # ================================================================== #
    def test_N7_ordinary_creation_still_succeeds_and_gets_a_well_formed_hash(self):
        """陽性對照：不碰撞的一般建立必須照常成功，並拿到合法的 8 hex ws_hash。

        沒有這條，N4-N6 的「被擋下」可能只是因為建立整條壞掉了（fail-closed 到把
        產品打死也算「攻擊被擋住」）。順便釘住格式：8 個小寫 hex ＝ 與
        `md5(...)[:8]` 同字集同長度 ⇒ K8s namespace（DNS-1123）與 operator 的
        `namespace.replace('paas-ws-','')` 契約都不變。
        """
        self.authenticate(self.attacker_login, self.attacker_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'create', [{'name': 'ordinary 677 ws'}])
        self._assert_ok(result, '一般建立必須成功')
        ws = self.Workspace.sudo().browse(result['result'])
        self.assertRegex(
            ws.ws_hash or '', r'^[0-9a-f]{8}$',
            'ws_hash 必須是 8 個小寫 hex（與舊 md5[:8] 同字集同長度）')
        self.assertNotEqual(
            ws.ws_hash, self.victim_ws.sudo().ws_hash, '不得與既有 workspace 相同')
        self.assertTrue(
            make_namespace(ws.ws_hash).startswith('paas-ws-'),
            'namespace 形狀不變')

    # ================================================================== #
    #  N8 —— 既有資料不漂移（migration 回填的契約）                        #
    # ================================================================== #
    def test_N8_a_backfilled_legacy_hash_reproduces_the_pre_fix_names_byte_for_byte(self):
        """🔴 回填成 `md5(slug)[:8]` 的既有 workspace，推導結果必須與修復前**逐字相同**。

        這是「改推導不會動到既有部署」的契約：migration
        （`18.0.1.0.129/post-migrate.py` step 3）把既有列回填成舊公式的結果，所以
        本測試模擬回填後，`make_namespace` / `make_service_subdomain` /
        `make_security_access_subdomain` 三者都必須吐出與修復前一模一樣的字串。

        期望值刻意用**修復前的公式**（`'paas-ws-' + generate_ws_hash(slug)`）現算，
        而不是抄一個字面常數——常數會在 slug 變動時假綠。
        """
        ws = self.Workspace.with_user(self.victim).create({'name': 'legacy 677 ws'})
        legacy = generate_ws_hash(ws.sudo().slug)
        # sudo 是 privileged writer ⇒ 可以寫 anchor，等同 migration 的回填。
        ws.sudo().write({'ws_hash': legacy})
        self.env.invalidate_all()

        self.assertEqual(ws.sudo().ws_hash, legacy, '回填必須落地')
        self.assertEqual(
            make_namespace(ws.sudo().ws_hash), 'paas-ws-' + legacy,
            'namespace 必須與修復前逐字相同')
        self.assertEqual(
            make_service_subdomain(ws.sudo().ws_hash, 'ref-1', 'svc-1'),
            'paas-cs-%s-%s' % (legacy, generate_resource_hash('ref-1', 'svc-1')),
            'CloudService 子網域必須與修復前逐字相同')
        self.assertEqual(
            make_security_access_subdomain(ws.sudo().ws_hash, 'ref-1', 'sa-1'),
            'paas-sa-%s-%s' % (legacy, generate_resource_hash('ref-1', 'sa-1')),
            'SecurityAccess 子網域必須與修復前逐字相同')

    # ================================================================== #
    #  N9 —— 絆線：naming helper 拿到 slug 必須 fail-loud                  #
    # ================================================================== #
    def test_N9_naming_helpers_refuse_anything_that_is_not_an_8_hex_hash(self):
        """🔴 漏改一個呼叫點會產出 `paas-ws-ws-137425` 這種**看起來很像對的**名字。

        這條是那個漏改的絆線：helper 收到非 8-hex 一律 raise，所以漏改的呼叫點在
        第一次執行時就炸，而不是安靜地部署到一個名字很像的 namespace。空值／None
        （欄位沒回填）同樣落在 raise 這一側 ＝ fail-closed。
        """
        for bad in (VICTIM_NAME, 'my-workspace', '', None, '7D82CE00', '7d82ce0', 12345):
            with self.assertRaises(ValueError, msg='make_namespace(%r) 必須 raise' % bad):
                make_namespace(bad)
            with self.assertRaises(ValueError, msg='make_service_subdomain(%r) 必須 raise' % bad):
                make_service_subdomain(bad, 'ref', 'name')
            with self.assertRaises(ValueError, msg='make_security_access_subdomain(%r) 必須 raise' % bad):
                make_security_access_subdomain(bad, 'ref', 'name')

        # 陽性對照：合法的 8-hex 不得被誤擋（否則上面的 raise 只是「什麼都擋」）。
        self.assertEqual(make_namespace(LEGACY_COLLIDING_HASH),
                         'paas-ws-' + LEGACY_COLLIDING_HASH)

    # ================================================================== #
    #  N10 —— §1.2 的例外：每次重新推導的路徑錨在 stored hash 上            #
    # ================================================================== #
    def test_N10_rederivation_is_anchored_on_the_stored_hash_not_on_the_slug(self):
        """🔴 `smart_home._generate_subdomain()` 是**每次呼叫重新推導**的；改了
        workspace 的 slug 之後，它必須算出**同一個**值。

        修復前它吃 `workspace.slug` ⇒ slug 一變、子網域就變，而已部署的 tunnel /
        DNS 記錄用的是舊值 ⇒ 靜默分岔。修復後它吃 stored `ws_hash` ⇒ 結構上不可能
        漂移。

        這比「盤點所有呼叫點、證明再推導不會發生」強：它不依賴盤點的完整性，日後
        任何人新增一條再推導路徑也自動安全。

        slug 用 `sudo()` 改（租戶被 #658 的 anchor 擋著改不了），因為這裡量的是
        「推導讀不讀 slug」，不是「誰能改 slug」。
        """
        before = self._smart_home_subdomain(self.victim_ws)
        sa_before = self._sa_canonical_name(self.victim_ws)

        self.victim_ws.sudo().write({'slug': 'a-completely-different-slug-677'})
        self.env.invalidate_all()
        self.assertEqual(
            self.victim_ws.sudo().slug, 'a-completely-different-slug-677',
            '前提：slug 真的被改掉了（否則下面的「沒有漂移」是恆真式）')

        self.assertEqual(
            self._smart_home_subdomain(self.victim_ws), before,
            'SmartHome 子網域不得隨 slug 漂移')
        self.assertEqual(
            self._sa_canonical_name(self.victim_ws), sa_before,
            'SecurityAccess canonical name 不得隨 slug 漂移')
