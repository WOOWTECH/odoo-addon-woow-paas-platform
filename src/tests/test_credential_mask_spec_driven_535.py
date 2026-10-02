"""#535：租戶填入的憑證（`helm_value_specs` 的 `type: password`）在服務詳情 API 明文回傳。

**為什麼要另立一檔、而且必須是宣告式的**

既有四個 sanitizer 呼叫點（``test_mcp_admin_password.py:65``、
``test_mcp_admin_password_authz.py:184/200/245``）全是「fixture 塞一個 magic string →
``assertNotIn``」的**形狀鎖定**：它們鎖的是「這一條我知道的路徑有沒有被遮」。於是
**新增任何一個 ``type: password`` 的 spec 都不會讓任何測試變紅**——這正是本缺陷從
seed 第一天起就存在、卻一路全綠的原因（#530 是同一個形狀的第二次發生）。

本檔改成從 **seed 本身**推導測試資料：讀每一個 template 的 ``helm_value_specs``、
取出所有 ``type == password`` 的 key、逐一注入可辨識明文再斷言被遮。新增一個
password 欄位而沒被涵蓋 ⇒ 這裡自動變紅，不需要有人記得回來補測試。

⚠ **floor 折進迴圈**：純粹「for spec in 清單: assert」有假綠洞——清單縮成空的
（例如推導方式自己壞掉、或 seed 被改動）就少跑幾輪、照樣綠。所以
``test_every_password_spec_in_seed_is_masked`` 在**同一條路徑上**同時斷言
「推導出來的集合涵蓋 anchor」，anchor 少一個就以「缺哪一個」的形式紅。
"""
import json

from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.paas import PaasController


# 推導結果的下限錨點：``{template slug: {spec key}}``。
#
# 這不是「目前有哪些 password 欄位」的快照（那會變成把當下事實寫死成契約），而是
# **推導機制的存活訊號**：本 issue 點名「odooPassword 是租戶 Odoo 實例的管理員密碼，
# 敏感度最高」，它從 seed 裡消失是重大變更、本來就該讓人重新看一次這裡。
# 反過來，新增 password 欄位**不需要**動這份 anchor——那正是宣告式的意義。
#
# #1130（#1160 Q1）：odooPassword 不是改鍵名而是**整個移除**——woow-odoo 的初始管理員
# 密碼改由平台鑄造、走 sensitive `conf.adminPassword`（不在 specs，見
# test_existing_hardcoded_paths_still_masked）；smtpPassword 隨 SMTP 欄位暫時移除、
# 由 #1193 以 dot-path `smtp.password` 補回。anchor 改錨在仍有租戶自填憑證的 template。
_ANCHOR_PASSWORD_SPECS = {
    'open-design': {'auth.anthropicApiKey', 'auth.openrouterApiKey'},
    'code-server': {'auth.gitToken'},
}


def _password_keys_of(template):
    """獨立於受測程式碼，自己把 template 的 password spec key 解出來。

    刻意**不呼叫** ``CloudAppTemplate._parse_helm_value_specs``（#1052 自 controller 搬入）
    / ``PaasController._password_spec_paths``：
    那是受測物的一部分，拿它產生測試資料的話，它壞掉時測試會一起退化成 0 輪迴圈
    （探針與被測物同時失效、輸出卻和「全部通過」一模一樣）。
    """
    raw = template.helm_value_specs
    if not raw:
        return []
    specs = json.loads(raw, strict=False)
    keys = []
    for group in ('required', 'optional'):
        for spec in specs.get(group) or []:
            if spec.get('type') == 'password' and spec.get('key'):
                keys.append(spec['key'])
    return keys


def _set_by_dotpath(values, key, value):
    """把 ``a.b.c`` 形式的 key 寫進巢狀 dict——與後端 ``_unflatten_dotpath_keys`` 同語意。"""
    parts = key.split('.')
    cursor = values
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value
    return values


class TestPasswordSpecsAreMaskedDeclaratively(TransactionCase):
    """遮罩必須以 ``helm_value_specs`` 為準，而不是逐條列舉路徑。"""

    def test_every_password_spec_in_seed_is_masked(self):
        """🔴 本 issue 的主守線：seed 裡每一個 ``type: password`` 都必須被遮。

        鑑別力（驗收條件 1 的注入式驗證就是打這條）：在
        ``src/data/cloud_app_templates.xml`` 新增一個 ``type: password`` 的 spec、
        而遮罩沒有涵蓋它 ⇒ 這條紅，且訊息直接指出是哪個 template 的哪個 key。
        """
        templates = self.env['woow_paas_platform.cloud_app_template'].search([])
        # 陽性對照：seed 真的載進這個 DB 了。沒有這條，一個空 DB 會讓下面整段
        # 退化成「0 個 template、0 個 spec、沒有洩漏」的假綠。
        self.assertTrue(templates, 'seed 的 cloud_app_template 一筆都沒有——測試環境不對')

        discovered = {}
        leaked = []
        for template in templates:
            keys = _password_keys_of(template)
            if not keys:
                continue
            discovered.setdefault(template.slug, set()).update(keys)

            values = {}
            markers = {}
            for index, key in enumerate(keys):
                marker = f'PLAINTEXT_{template.slug}_{index}_DO_NOT_LEAK'.upper()
                markers[key] = marker
                _set_by_dotpath(values, key, marker)

            sanitized = json.dumps(
                PaasController._sanitize_helm_values(values, template))
            for key, marker in markers.items():
                if marker in sanitized:
                    leaked.append(f'{template.slug}.{key}')

        # floor 折進同一條路徑：推導出來的集合必須涵蓋 anchor。少一個就以
        # 「缺哪一個」的形式紅——而不是「少跑一輪、照樣綠」。
        # 用集合差報「是哪一個」而不是報「差幾個」：數字沒有可證偽性。
        missing_anchor = {
            f'{slug}.{key}'
            for slug, keys in _ANCHOR_PASSWORD_SPECS.items()
            for key in keys
            if key not in discovered.get(slug, set())
        }
        self.assertFalse(
            missing_anchor,
            f'從 seed 推導 password spec 的機制失效或 seed 被改動，缺少：'
            f'{sorted(missing_anchor)}；實際推導到 {discovered}')

        self.assertFalse(
            leaked,
            f'以下 type: password 的欄位在遮罩後仍以明文外流：{sorted(leaked)}。'
            f'遮罩必須以 helm_value_specs 為準——新增一個 password spec 不該需要'
            f'有人記得回來補 controller 的路徑清單。')

    def test_a_brand_new_password_key_is_masked_without_touching_the_controller(self):
        """🔴 「遮罩真的讀了 ``helm_value_specs``」的存在性檢查。

        上面那條宣告式測試有一個殘留缺口：如果有人把 spec-driven 改回**寫死今天這
        6 把 key** 的清單，它照樣全綠——要等到有人新增第 7 個 password spec 才會紅。
        本條用一個**現實中不存在、任何寫死清單都不可能命中**的 key，直接證明遮罩是
        在執行期讀 specs 推導的。

        比「用 AST 檢查 controller 有沒有呼叫 `_password_spec_paths`」強：那種 gate 守的
        是單一語法形態，留著呼叫但不使用回傳值照樣過；這條守的是**行為**。

        鑑別力：把 spec-driven 分支換成任何 hardcoded 清單 ⇒ 這條紅。
        """
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Novel credential key',
            'slug': 'novel-credential-key-535',
            'category': 'web',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/novel',
            'helm_chart_version': '0.1.0',
            'helm_value_specs': json.dumps({'required': [
                {'key': 'aKeyNobodyHardcoded', 'label': 'Novel', 'type': 'password'},
                {'key': 'nested.alsoNovelSecret', 'label': 'Novel nested',
                 'type': 'password'},
                {'key': 'nested.plainSetting', 'label': 'Not a credential',
                 'type': 'text'},
            ]}),
        })

        sanitized = PaasController._sanitize_helm_values({
            'aKeyNobodyHardcoded': 'NOVEL_PLAINTEXT_DO_NOT_LEAK',
            'nested': {'alsoNovelSecret': 'NOVEL_NESTED_DO_NOT_LEAK',
                       'plainSetting': 'keep-me'},
        }, template)

        dumped = json.dumps(sanitized)
        self.assertNotIn('NOVEL_PLAINTEXT_DO_NOT_LEAK', dumped,
                         '遮罩必須在執行期從 helm_value_specs 推導憑證鍵')
        self.assertNotIn('NOVEL_NESTED_DO_NOT_LEAK', dumped,
                         'dot-path 形態的新 key 同樣要涵蓋')
        self.assertEqual(sanitized['nested']['plainSetting'], 'keep-me',
                         '同層的非 password spec 不得被連坐')

    def test_mask_does_not_spill_onto_non_credential_siblings(self):
        """遮罩要精準命中憑證那一格，不得連坐同層的非憑證欄位。

        PostgreSQL 家族的 seed 有 ``auth.username`` / ``auth.database``，與
        ``auth.*ApiKey`` 同層。整個 ``auth`` 子樹全遮會讓那些 template 的設定分頁
        變成一排 ``***``（#530 已經踩過一次）。

        鑑別力：把 spec-driven 分支改成遮整個父節點 ⇒ 這條紅。
        """
        template = self.env.ref('woow_paas_platform.cloud_app_opendesign')
        sanitized = PaasController._sanitize_helm_values({
            'auth': {
                'anthropicApiKey': 'ANTHROPIC_PLAINTEXT_DO_NOT_LEAK',
                'username': 'bn_odoo',
                'database': 'bitnami_odoo',
            },
        }, template)

        self.assertNotIn(
            'ANTHROPIC_PLAINTEXT_DO_NOT_LEAK', json.dumps(sanitized),
            '憑證那一格必須被遮')
        self.assertEqual(sanitized['auth']['username'], 'bn_odoo',
                         '非憑證的同層欄位不得被連坐遮掉')
        self.assertEqual(sanitized['auth']['database'], 'bitnami_odoo',
                         '非憑證的同層欄位不得被連坐遮掉')

    def test_existing_hardcoded_paths_still_masked(self):
        """三條既有路徑是**增加一層**、不是被 spec-driven 取代。

        它們的 key 不在 ``helm_value_specs`` 裡（平台自己注入，租戶看不到也填不到），
        spec-driven 涵蓋不到 ⇒ 拿掉任何一條都會開一個新洞，而本檔其他測試不會紅。
        """
        template = self.env.ref('woow_paas_platform.cloud_app_hermes_agent')
        sanitized = PaasController._sanitize_helm_values({
            'config': {'sensitive': {'admin_password': 'ADMIN_PW_DO_NOT_LEAK'}},
            'auth': {'apiServerKey': 'GATEWAY_KEY_DO_NOT_LEAK'},
        }, template)
        dumped = json.dumps(sanitized)

        self.assertNotIn('ADMIN_PW_DO_NOT_LEAK', dumped,
                         'config.sensitive 這條既有路徑不得因為改 spec-driven 而失守')
        self.assertNotIn('GATEWAY_KEY_DO_NOT_LEAK', dumped,
                         'auth.apiServerKey（#530）這條既有路徑不得失守')

    def test_woow_odoo_admin_password_is_masked_without_its_siblings(self):
        """#1130：woow-odoo 的 ``conf.adminPassword`` 是平台注入的第四條固定路徑。

        它不在 ``helm_value_specs``（平台鑄造、租戶不填），spec-driven 涵蓋不到；正常
        路徑已被 ``_strip_mcp_sensitive_values`` 擋在落庫前，這裡鎖第二道。同層
        ``conf.adminEmail``／``conf.workers`` 是設定，不得被連坐。
        """
        template = self.env.ref('woow_paas_platform.cloud_app_odoo')
        sanitized = PaasController._sanitize_helm_values({
            'conf': {'adminPassword': 'ODOO_ADMIN_PW_DO_NOT_LEAK',
                     'adminEmail': 'owner@example.com', 'workers': 2},
        }, template)

        self.assertNotIn('ODOO_ADMIN_PW_DO_NOT_LEAK', json.dumps(sanitized))
        self.assertEqual(sanitized['conf']['adminPassword'], '***')
        self.assertEqual(sanitized['conf']['adminEmail'], 'owner@example.com')
        self.assertEqual(sanitized['conf']['workers'], 2)

    def test_legacy_secret_subtree_keeps_its_eight_char_prefix(self):
        """#535 的明確裁決：legacy ``secret`` 子樹的 8 字元前綴契約**維持不變**。

        ``test_mcp_admin_password_authz.py:193-205`` 把這件事寫成契約（理由：n8n 既有
        UI 靠前綴辨識是哪一把 key）。本 issue 採「spec-driven 是加一層」而不是「統一
        全遮」，所以那條契約不受影響——這裡把同一件事在本檔再鎖一次，讓「#535 有沒有
        偷偷改掉那條測試」在本檔就看得出來。

        ``secret.*`` 的 key 不在 ``helm_value_specs`` 裡，spec-driven 本來也涵蓋不到它。
        """
        template = self.env.ref('woow_paas_platform.cloud_app_n8n')
        sanitized = PaasController._sanitize_helm_values(
            {'secret': {'N8N_API_KEY': 'abcdefghijklmnop'}}, template)

        self.assertEqual(
            sanitized['secret']['N8N_API_KEY'], 'abcdefgh...',
            'legacy secret 子樹維持 8 字元前綴（n8n UI 依賴），#535 不改動它')


class TestMaskFailsClosed(TransactionCase):
    """無從得知哪些鍵是憑證時，全遮而不是全放。"""

    def test_missing_template_masks_everything(self):
        """``template`` 為空 ⇒ 全遮。

        修復前這個函式收了 ``template`` 參數卻整段沒用到——傳什麼進去輸出都一樣，
        連「傳錯 template」都沒有任何測試會紅。
        """
        sanitized = PaasController._sanitize_helm_values(
            {'somePassword': 'PLAINTEXT_DO_NOT_LEAK',
             'nested': {'token': 'ALSO_PLAINTEXT_DO_NOT_LEAK'}}, None)

        dumped = json.dumps(sanitized)
        self.assertNotIn('PLAINTEXT_DO_NOT_LEAK', dumped)
        self.assertNotIn('ALSO_PLAINTEXT_DO_NOT_LEAK', dumped)

    def test_unparseable_specs_mask_everything(self):
        """``helm_value_specs`` 讀不懂 ⇒ 全遮。

        注意這與「template 本來就沒有 specs」是**不同**的狀況，見下一條——
        ``_parse_helm_value_specs`` 把兩者都壓成 ``{}``，所以判定端必須用
        ``_parse_helm_value_specs_or_fail`` 的哨兵分辨。
        """
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Broken specs',
            'slug': 'broken-specs-535',
            'category': 'web',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/broken',
            'helm_chart_version': '0.1.0',
            'helm_value_specs': '{"required": [ THIS IS NOT JSON',
        })

        sanitized = PaasController._sanitize_helm_values(
            {'somePassword': 'PLAINTEXT_DO_NOT_LEAK'}, template)

        self.assertNotIn('PLAINTEXT_DO_NOT_LEAK', json.dumps(sanitized))

    def test_template_without_specs_is_not_blanket_masked(self):
        """反向守線：**沒有** specs 是正常狀態，不得因此把整包遮掉。

        ``cloud_app_ha_paas`` 就是這種 template（零 ``helm_value_specs``）。把
        fail-closed 寫成「specs 是空的就全遮」會讓它的設定顯示變成一排 ``***``——
        這條就是那個過度反應的守線。既有的三條 hardcoded path 在這裡仍然有效。
        """
        template = self.env.ref('woow_paas_platform.cloud_app_ha_paas')
        self.assertFalse(
            template.helm_value_specs,
            'fixture 前提：這個 template 本來就沒有 helm_value_specs')

        sanitized = PaasController._sanitize_helm_values(
            {'hostname': 'ha.example.com',
             'config': {'sensitive': {'admin_password': 'ADMIN_PW_DO_NOT_LEAK'}}},
            template)

        self.assertEqual(sanitized['hostname'], 'ha.example.com',
                         '非憑證欄位不得因為 template 沒有 specs 就被全遮')
        self.assertNotIn('ADMIN_PW_DO_NOT_LEAK', json.dumps(sanitized),
                         '既有的 config.sensitive 路徑在無 specs 的 template 上仍須生效')

    def test_malformed_specs_structure_masks_everything(self):
        """specs 解析得出來但結構壞掉（group 不是 list 等）⇒ 同樣 fail-closed。

        「只解析出一部分憑證鍵」的輸出與「全部解析成功」長得一模一樣，會靜默漏遮，
        所以結構異常不做 best-effort 略過。
        """
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Malformed specs',
            'slug': 'malformed-specs-535',
            'category': 'web',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/malformed',
            'helm_chart_version': '0.1.0',
            'helm_value_specs': '{"required": "not-a-list"}',
        })

        sanitized = PaasController._sanitize_helm_values(
            {'somePassword': 'PLAINTEXT_DO_NOT_LEAK'}, template)

        self.assertNotIn('PLAINTEXT_DO_NOT_LEAK', json.dumps(sanitized))


class TestReconfigureDoesNotWriteTheMaskBack(TransactionCase):
    """🔴 read 端遮罩與 write 端回填是**同一個契約**。

    只改後端遮罩、不改前端，租戶按一次存檔就會把星號當成自己的輸入送回來，
    ``_deep_merge`` 讓它覆蓋既有值 ⇒ **平台把租戶的 Odoo 管理員密碼改成 ``'***'``**。
    這在真瀏覽器上實測過（#535 D1：``odooPassword`` 從 ``PLAIN_PW_D1_PROBE`` 被寫成
    ``'***'``；同一套操作在沒有遮罩的世界則保住明文）。

    主修法在前端（``ConfigurationTab`` 密碼欄空白起始）。本 class 鎖的是**後端兜底**
    ——前端 bundle 與後端不是原子部署，升級當下已開著分頁的租戶跑的還是舊 JS。
    """

    def setUp(self):
        super().setUp()
        # #1130：原 fixture 是 odoo 的 odooPassword／smtpHost；odoo 表單已不收任何密碼
        # （#1160），改用仍有租戶自填憑證的 open-design。鍵以扁平 spec key 送、巢狀落庫，
        # 與真實前端／helm_values 的形態相同。
        self.template = self.env.ref('woow_paas_platform.cloud_app_opendesign')
        self.existing = {
            'auth': {
                'anthropicApiKey': 'REAL_PASSWORD_DO_NOT_OVERWRITE',
                'username': 'woow',
            },
        }

    def test_echoed_mask_is_dropped_so_the_credential_survives(self):
        """送回來的值 == 剛才顯示給他看的遮罩值 ⇒ 這格沒有變更。"""
        # 前提斷言：先證明 API 真的會把它顯示成遮罩值，否則下面的「送回遮罩值」
        # 只是憑空造的字串，這條測試會在「遮罩根本沒生效」的世界照樣綠。
        shown = PaasController._sanitize_helm_values(
            json.loads(json.dumps(self.existing)), self.template)
        self.assertEqual(shown['auth']['anthropicApiKey'], '***',
                         '前提：service detail API 會把 auth.anthropicApiKey 顯示成 ***')

        kept = PaasController._drop_masked_echo_values(
            {'auth.username': 'woow', 'auth.anthropicApiKey': '***'},
            self.existing, self.template)

        self.assertNotIn(
            'auth.anthropicApiKey', kept,
            '原封送回的遮罩值必須被丟棄——留著它會經 _deep_merge 把真憑證覆蓋成 ***')
        self.assertEqual(kept['auth.username'], 'woow',
                         '非憑證欄位照常通過')

    def test_a_real_new_password_is_kept(self):
        """陽性對照：使用者真的輸入新密碼時**必須**存得進去。

        少了這條，「密碼再也改不了」這個 regression 會被上面那條測試當成成功。
        """
        kept = PaasController._drop_masked_echo_values(
            {'auth.anthropicApiKey': 'A_BRAND_NEW_PASSWORD'}, self.existing, self.template)

        self.assertEqual(kept['auth.anthropicApiKey'], 'A_BRAND_NEW_PASSWORD')

    def test_non_credential_field_may_legitimately_equal_the_mask_literal(self):
        """不連坐：規則是「等於顯示值**且**與實值不同」，不是「等於 ``'***'``」。

        沒被遮罩的欄位顯示值就是實值，條件恆假 ⇒ 租戶要把 auth.username 設成
        ``'***'`` 照樣可以。只比對字面 ``'***'`` 的實作會擋掉它 ⇒ 這條紅。
        """
        kept = PaasController._drop_masked_echo_values(
            {'auth.username': '***'}, self.existing, self.template)

        self.assertEqual(kept['auth.username'], '***',
                         '非憑證欄位設成 *** 是合法的，不得被兜底規則連坐')

    def test_missing_key_preserves_the_existing_value(self):
        """缺 key ＝保留既有，不可當成「清空」。

        前端修法讓密碼欄留白時**不送**該 key，所以後端這一半的語意必須是保留。
        """
        merged = PaasController._deep_merge(
            self.existing,
            PaasController._unflatten_dotpath_keys({'auth.username': 'woow2'}))

        self.assertEqual(merged['auth']['anthropicApiKey'], 'REAL_PASSWORD_DO_NOT_OVERWRITE',
                         '沒有送出的欄位必須保留既有值')
        self.assertEqual(merged['auth']['username'], 'woow2')

    def test_dotpath_credentials_are_dropped_too(self):
        """dot-path 形態（``auth.anthropicApiKey``）也要走同一條兜底。

        前端送出的是**扁平** spec key，而 helm_values 裡是巢狀結構——兩邊的查法
        必須一致，只實作其中一種會對另一種誤判成「這個鍵不存在」而放行遮罩值。
        """
        template = self.env.ref('woow_paas_platform.cloud_app_opendesign')
        existing = {'auth': {'anthropicApiKey': 'REAL_KEY_DO_NOT_OVERWRITE',
                             'username': 'woow'}}

        kept = PaasController._drop_masked_echo_values(
            {'auth.anthropicApiKey': '***'}, existing, template)

        self.assertNotIn('auth.anthropicApiKey', kept,
                         'dot-path 形態的憑證同樣不得被星號覆蓋')

    def test_flat_stored_credentials_are_dropped_too(self):
        """舊資料可能以字面 ``'auth.anthropicApiKey'`` 當鍵落庫（前端 getSpecValue
        就是先試 flat key 再試 dot-path）——那一種形態也要涵蓋。"""
        template = self.env.ref('woow_paas_platform.cloud_app_opendesign')
        existing = {'auth.anthropicApiKey': 'REAL_KEY_DO_NOT_OVERWRITE'}

        # 前提：扁平落庫的憑證也真的會被遮罩顯示成 ***。
        shown = PaasController._sanitize_helm_values(dict(existing), template)
        self.assertEqual(shown['auth.anthropicApiKey'], '***')

        kept = PaasController._drop_masked_echo_values(
            {'auth.anthropicApiKey': '***'}, existing, template)
        self.assertNotIn('auth.anthropicApiKey', kept)
