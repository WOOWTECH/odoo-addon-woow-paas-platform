# -*- coding: utf-8 -*-
"""Home Assistant 完整版（APPORO）隱藏卡片的守線斷言。
這張卡是 ha-full 的品牌變體：同 chart、同 values、同 plan，只有 image 換成
apporo-ha-full。守線的重點是「除 image 外必須與 ha-full 逐鍵相同」——兩張卡若各自漂
走，展示機的 6 台 APPORO 與 6 台 WOOWTECH 會跑在不同的形狀上，而沒有任何東西會報錯。
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

    def test_hidden_but_deployable(self):
        """未上架（is_hidden）但既有服務照常可用（不設 conceal_instances）——#471 兩層語意。"""
        tpl = self._apporo()
        self.assertTrue(tpl.is_hidden, 'APPORO 卡不上架')
        self.assertFalse(tpl.conceal_instances, 'APPORO 展示機的既有服務必須可見可操作')
        self.assertTrue(tpl.is_active)
        self.assertTrue(tpl.subscription_plan_ids, '沒掛 plan 的 template 開不了服務（paas.py 檢查 plan in subscription_plan_ids）')

    def test_chart_lockstep_with_ha_full(self):
        """chart 座標與版號必須和 ha-full 完全一致——同一個 chart 不能有兩個版本在跑。"""
        a, w = self._apporo(), self._woowtech()
        self.assertEqual(a.helm_repo_url or '', '')
        self.assertEqual(a.helm_chart_name, w.helm_chart_name)
        self.assertEqual(a.helm_chart_version, w.helm_chart_version)

    def test_values_equal_except_image(self):
        a = json.loads(self._apporo().helm_default_values)
        w = json.loads(self._woowtech().helm_default_values)
        a.pop('image'); w.pop('image')
        self.assertEqual(a, w, 'APPORO 卡的 values 除 image 外必須與 ha-full 逐鍵相同')

    def test_image_is_apporo_full_line(self):
        img = json.loads(self._apporo().helm_default_values)['image']
        self.assertEqual(img['repository'], APPORO_IMAGE)
        self.assertIn('-full', img['repository'])
        self.assertIn('apporo', img['repository'])

    def test_image_tag_pinned_digest(self):
        tag = json.loads(self._apporo().helm_default_values)['image']['tag']
        self.assertNotEqual(tag, 'latest')
        self.assertIn('@sha256:', tag, 'image tag 必須雙釘 tag@sha256')
        build_tag, digest = tag.split('@', 1)
        self.assertRegex(build_tag, r'^\d{8}\.\d{4}$')
        self.assertEqual(len(digest), len('sha256:') + 64, digest)

    def test_same_plans_as_ha_full(self):
        self.assertEqual(set(self._apporo().subscription_plan_ids.ids),
                         set(self._woowtech().subscription_plan_ids.ids))
