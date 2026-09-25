# -*- coding: utf-8 -*-
"""Home Assistant 完整版（APPORO）—— 封存卡片的守線。

這張卡當初只為一件事而建：把 woowhademo 的 6 台 APPORO 展示機收進 PaaS 管理。
使用者 2026-09-25 決定那 6 台不走 PaaS（改掛 apporo.ai 的 Cloudflare 部署、稍後移機），
於是 seed 把它 `is_active=False` 封存——留記錄、不刪，因為它從未開出任何服務。

本檔案的形狀刻意是「二擇一」：

* **封存中**（現況）→ 只斷言封得住：記錄還在、市集 domain 撈不到、沒有任何服務掛上去。
  刻意**不**再斷言它與 ha-full 的 chart 版號／values lockstep——封存的東西不該在
  ha-full 每次升版時把人叫醒。
* **一旦有人翻回 `is_active=True`** → 同一條測試自動改跑原本那組 lockstep 斷言
  （除 image 外逐鍵相同、chart 版號一致、tag 雙釘、plan 與 ha-full 相同）。

為什麼要這樣寫：解封是營運決策，但「解封」與「解封後兩張卡不能各自漂走」是兩件事。
如果封存時直接把 lockstep 刪掉，某天翻旗標的人不會知道那些保證存在過，6 台 APPORO 與
6 台 WOOWTECH 就會跑在不同形狀上而沒有任何東西會報錯（那正是本檔案最早存在的理由）。
"""
import json

from odoo.tests.common import TransactionCase, tagged

APPORO_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/apporo-ha-full'


@tagged('post_install', '-at_install')
class TestHaFullApporoSeed(TransactionCase):

    def _apporo(self):
        return self.env.ref('woow_paas_platform.cloud_app_ha_full_apporo')

    def _woowtech(self):
        return self.env.ref('woow_paas_platform.cloud_app_ha_full')

    # ------------------------------------------------------------------ #
    #  封存守線                                                           #
    # ------------------------------------------------------------------ #
    def test_record_survives_the_seal(self):
        """封存 ≠ 刪除：xmlid 必須仍解得開，否則「翻回來就好」這條退路不存在。"""
        tpl = self._apporo()
        self.assertTrue(tpl.exists())
        self.assertEqual(tpl.slug, 'ha-full-apporo')

    def test_seal_actually_bites(self):
        """用 controller 自己的市集 domain 驗，不是覆述旗標。

        判準抄自 `controllers/paas.py` 的商品列表
        （`[('is_active','=',True), ('is_hidden','=',False)]`）——規格頁與 provision
        端點另外三處也各自擋一次 `not template.is_active or template.is_hidden`。
        這裡斷言的是**行為**：封存後這張卡不可能出現在任何人的市集裡。
        """
        Tpl = self.env['woow_paas_platform.cloud_app_template'].sudo()
        listed = Tpl.search([('is_active', '=', True), ('is_hidden', '=', False)])
        self.assertNotIn(
            self._apporo().id, listed.ids,
            'APPORO 卡已封存，不該出現在市集列表')

    def test_no_service_was_ever_created(self):
        """封存的安全前提：沒有租戶站在這張卡上。

        真有服務掛著卻把卡封了，那些服務的續訂／改方案路徑會踩到「卡片不可用」，
        屆時該做的是先遷服務、不是封卡——所以這條紅了就是封錯了，不是測試要改。
        """
        n = self.env['woow_paas_platform.cloud_service'].sudo().search_count(
            [('template_id', '=', self._apporo().id)])
        self.assertEqual(n, 0, 'APPORO 卡上有服務，不能封存')

    # ------------------------------------------------------------------ #
    #  解封後才生效的原 lockstep 守線                                      #
    # ------------------------------------------------------------------ #
    def test_sealed_or_else_lockstep_with_ha_full(self):
        """封存中就只確認封存；一旦解封，原本那組 lockstep 立刻回來生效。"""
        a = self._apporo()
        if not a.is_active:
            self.assertTrue(
                a.is_hidden,
                '封存的卡不該同時被標成「已上架」——解封前請先決定它要不要賣')
            return

        # ↓ 以下是 2026-09-25 封存前的原始守線，原封不動搬過來。
        w = self._woowtech()
        self.assertFalse(a.conceal_instances, 'APPORO 展示機的既有服務必須可見可操作')
        self.assertTrue(
            a.subscription_plan_ids,
            '沒掛 plan 的 template 開不了服務（paas.py 檢查 plan in subscription_plan_ids）')
        self.assertEqual(set(a.subscription_plan_ids.ids),
                         set(w.subscription_plan_ids.ids))

        self.assertEqual(a.helm_repo_url or '', '')
        self.assertEqual(a.helm_chart_name, w.helm_chart_name)
        self.assertEqual(a.helm_chart_version, w.helm_chart_version,
                         '同一個 chart 不能有兩個版本在跑')

        av, wv = json.loads(a.helm_default_values), json.loads(w.helm_default_values)
        img = av['image']
        av.pop('image'); wv.pop('image')
        self.assertEqual(av, wv, 'APPORO 卡的 values 除 image 外必須與 ha-full 逐鍵相同')

        self.assertEqual(img['repository'], APPORO_IMAGE)
        self.assertIn('-full', img['repository'])
        self.assertIn('apporo', img['repository'])

        tag = img['tag']
        self.assertNotEqual(tag, 'latest')
        self.assertIn('@sha256:', tag, 'image tag 必須雙釘 tag@sha256')
        build_tag, digest = tag.split('@', 1)
        self.assertRegex(build_tag, r'^\d{8}\.\d{4}$')
        self.assertEqual(len(digest), len('sha256:') + 64, digest)
