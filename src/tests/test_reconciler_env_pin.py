"""TS-4 S2：L4 reconciler env-pin self-check（R3）+ F-ENV-1b 両端一致 + shared_tunnel_id 必填。

reconcile_orphans 入口 self-check（在打 operator / 蒐左集前 fail fast）：

- **R3 env-pin（最關鍵，擋 Q2②）**：讀 Odoo ``woow_paas_platform.deploy_env``，**不帶
  default**（禁 ``get_param(key,'prod')`` fallback）；值不在 {prod,stg} → abort 零候選。
  禁 fallback 的理由（已驗）：``res_config_settings``/``smart_home``/``security_access`` 既有讀取
  點都帶 ``,'prod'`` default → 「漏設」偽裝成「成功讀 prod」→ naive self-check 的 abort 分支
  永遠跑不到（su-env-exempt-false-green 同家族）。故本檔負向測試**主動 unlink** 該 param，
  確保真的測到 abort，不被 fixture 預設遮蔽。
- **F-ENV-1b 両端一致**：``inventory.paas_env`` 必須 == Odoo ``deploy_env``，否則 abort
  （抓「Odoo 自認 env A 卻打到 paas_env=B 的 operator」這類部署接線錯置）。
- **shared_tunnel_id 必填**：operator 缺報 → abort（無法保證 shared tunnel 本體不被誤判 →
  寧可 fail-closed，不靜默放行；與 operator schema 端 shared_tunnel_id 必填對稱）。

設計 doc §C.4（1/2/5）、Phase C.1（self-check）/C.2。
"""
from odoo.tests import TransactionCase, tagged

from ..services.reconciler import reconcile_orphans


class _FakeClient:
    """最小 operator client 替身：回固定 inventory（不真打 HTTP）。"""

    def __init__(self, inventory):
        self._inv = inventory

    def get_reconcile_inventory(self):
        return self._inv


@tagged('post_install', '-at_install')
class TestReconcileEnvPinSelfCheck(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ICP = cls.env['ir.config_parameter'].sudo()
        # operator 設定齊（client=None 時 get_paas_operator_client 不會先 None-abort），
        # 把焦點留給 env-pin self-check。本檔一律顯式傳 client 替身故其實用不到，但仍設好。
        cls.ICP.set_param('woow_paas_platform.operator_url', 'http://op:8000')
        cls.ICP.set_param('woow_paas_platform.operator_api_key', 'k')

    def _good_inventory(self, paas_env='stg', shared='shared-tun-uuid-0000'):
        return {
            'paas_env': paas_env,
            'shared_tunnel_id': shared,
            'partial': False,
            'failed_kinds': [],
            'resources': [],
        }

    def _set_deploy_env(self, val):
        self.ICP.set_param('woow_paas_platform.deploy_env', val)

    def _unset_deploy_env(self):
        # 主動移除，確保「未設定」真的測得到 abort（不被 fixture / 安裝預設遮蔽）。
        self.ICP.search([('key', '=', 'woow_paas_platform.deploy_env')]).unlink()

    # ── R3 env-pin ───────────────────────────────────────────────────────
    # 用 assertRaisesRegex 釘住 abort **來源是 env-pin**（非他處）：否則此測試對它具名要守的回歸
    # 向量（env-pin 被改回 get_param(...,'prod') fallback）沒有鑑別力——fallback 後 deploy_env=
    # 'prod'，仍會在後面的「両端一致」處 raise，泛 assertRaises 照過、回歸被遮蔽（critique #6）。
    # paas_env 故意設 'prod'（= 危險 fallback 預設值）：若 env-pin 回歸成 prod-fallback，
    # deploy_env='prod'==paas_env='prod' 両端一致也會通過 → 流程跑到底不 raise → 此測試紅、抓到回歸。
    def test_deploy_env_unset_aborts(self):
        """deploy_env 從未設定 → get_param 無 default 回 False → 由 **env-pin** abort，不 fallback prod。"""
        self._unset_deploy_env()
        client = _FakeClient(self._good_inventory(paas_env='prod'))
        with self.assertRaisesRegex(Exception, 'env-pin'):
            reconcile_orphans(self.env, client=client)

    def test_deploy_env_empty_aborts(self):
        """deploy_env='' → env-pin abort。"""
        self._set_deploy_env('')
        client = _FakeClient(self._good_inventory(paas_env='prod'))
        with self.assertRaisesRegex(Exception, 'env-pin'):
            reconcile_orphans(self.env, client=client)

    def test_deploy_env_illegal_value_aborts(self):
        """deploy_env 非白名單（'dev'）→ env-pin abort（只認 exactly prod|stg）。"""
        self._set_deploy_env('dev')
        client = _FakeClient(self._good_inventory(paas_env='dev'))
        with self.assertRaisesRegex(Exception, 'env-pin'):
            reconcile_orphans(self.env, client=client)

    # ── F-ENV-1b 両端一致 ────────────────────────────────────────────────
    def test_two_end_env_mismatch_aborts(self):
        """deploy_env=stg（env-pin 過）但 operator inventory.paas_env=prod（部署接錯）→ 由**両端一致** abort。"""
        self._set_deploy_env('stg')
        client = _FakeClient(self._good_inventory(paas_env='prod'))
        with self.assertRaisesRegex(Exception, '兩端'):
            reconcile_orphans(self.env, client=client)

    # ── shared_tunnel_id 必填 ────────────────────────────────────────────
    def test_missing_shared_tunnel_id_fails_loud(self):
        """inventory 缺 shared_tunnel_id 鍵 → _parse_inventory fail-loud（與 operator schema 必填對稱）。"""
        self._set_deploy_env('stg')
        inv = self._good_inventory()
        del inv['shared_tunnel_id']
        client = _FakeClient(inv)
        with self.assertRaisesRegex(Exception, 'shared_tunnel_id'):
            reconcile_orphans(self.env, client=client)

    def test_empty_shared_tunnel_id_aborts(self):
        """shared_tunnel_id=''（env-pin + 両端一致 都過）→ 由 shared_tunnel_id 必填 abort。"""
        self._set_deploy_env('stg')
        client = _FakeClient(self._good_inventory(shared=''))
        with self.assertRaisesRegex(Exception, 'shared_tunnel_id'):
            reconcile_orphans(self.env, client=client)

    # ── happy path（非 vacuity：確認守衛通過時真的會產出，不是恆 raise）──
    def test_happy_path_pinned_and_consistent_succeeds(self):
        """deploy_env=stg + inventory.paas_env=stg + shared_tunnel_id 有值 → 正常產出、不 abort。"""
        self._set_deploy_env('stg')
        client = _FakeClient(self._good_inventory(paas_env='stg', shared='shared-tun-uuid-0000'))
        result = reconcile_orphans(self.env, client=client)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.orphan_candidates, [], "空 inventory → 無孤兒候選")
