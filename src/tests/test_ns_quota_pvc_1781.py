"""#1781 PVC 上限也走不降額（``services/ns_quota_policy``，純函式）。

#1189 之前的止血：operator 回報支援 ``pvc_limit``（GET ``managed_quota.supports_pvc_limit``）時，
平台把 PVC 基準 10 納入目標，合併後送 ``max(10, 叢集現值)``；不支援時不送、照舊因 PVC
高於 10 判為 ``would_lower``（舊 operator 會靜默忽略未知欄位、照樣寫 10）。
"""
from odoo.tests.common import BaseCase, tagged

from ..services.ns_quota_policy import (
    build_hard, merge_never_lower, plan_safe_lower, supports_pvc_limit, with_pvc_baseline,
)
from .test_ns_quota_safe_lower_1681 import _hard, _managed, _target


def _capable(hard, **extra):
    return _managed(hard, supports_pvc_limit=True, **extra)


@tagged('post_install', '-at_install')
class TestPvcNeverLower1781(BaseCase):

    def _plan(self, managed, target=None):
        return plan_safe_lower(target or _target('18', '8Gi', '100Gi'), managed)

    def test_cluster_pvc_above_ten_is_kept_and_nothing_is_unsafe(self):
        plan = self._plan(_capable(_hard('8', '8Gi', '100Gi', pvc='12')))
        self.assertEqual(plan['payload']['pvc_limit'], '12')
        self.assertEqual(plan['lowering_unsafe'], [])
        self.assertEqual(plan['blocked'], [])
        self.assertEqual(build_hard(plan['payload'])['persistentvolumeclaims'], '12')

    def test_cluster_pvc_above_ten_is_not_a_pending_decrease(self):
        """基準 10 不是目標：叢集高於它不顯示成「待處理降額」（不會也不能降）。"""
        plan = self._plan(_capable(_hard('8', '8Gi', '100Gi', pvc='12')))
        self.assertNotIn('persistentvolumeclaims',
                         [key for key, _live, _wanted in plan['pending_decrease']])

    def test_cluster_pvc_at_or_below_ten_or_absent_sends_ten(self):
        for pvc in ('8', '10'):
            with self.subTest(pvc=pvc):
                plan = self._plan(_capable(_hard('8', '8Gi', '100Gi', pvc=pvc)))
                self.assertEqual(plan['payload']['pvc_limit'], '10')
                self.assertEqual(plan['lowering_unsafe'], [])
        hard = _hard('8', '8Gi', '100Gi')
        del hard['persistentvolumeclaims']
        plan = self._plan(_capable(hard))
        self.assertEqual(plan['payload']['pvc_limit'], '10')

    def test_spec_and_status_take_the_larger_pvc(self):
        managed = _capable(_hard('8', '8Gi', '100Gi', pvc='10'))
        managed['hard']['persistentvolumeclaims'] = '12'
        self.assertEqual(self._plan(managed)['payload']['pvc_limit'], '12')

    def test_unreadable_cluster_pvc_is_not_provable(self):
        plan = self._plan(_capable(_hard('8', '8Gi', '100Gi', pvc='lots')))
        self.assertIsNone(plan['payload'])
        self.assertEqual(plan['blocked'][0][0], '*')
        self.assertTrue(plan['blocked'][0][1].startswith('quota_not_provable'))

    def test_old_operator_gets_no_pvc_field_and_still_blocks(self):
        """GET 沒有能力鍵：不送 pvc_limit，照舊判 would_lower（平台不得以為送了 12）。"""
        for flag in (None, False, 'true', 1):
            with self.subTest(flag=flag):
                managed = _managed(_hard('8', '8Gi', '100Gi', pvc='12'))
                if flag is not None:
                    managed['supports_pvc_limit'] = flag
                self.assertFalse(supports_pvc_limit(managed))
                plan = self._plan(managed)
                self.assertNotIn('pvc_limit', plan['payload'])
                self.assertEqual(plan['lowering_unsafe'], ['persistentvolumeclaims'])

    def test_payload_keeps_every_existing_field(self):
        plan = self._plan(_capable(_hard('8', '8Gi', '100Gi', '32Gi', '400Gi', pvc='12')))
        self.assertEqual(plan['payload'], {
            'cpu_limit': '18', 'memory_limit': '8Gi', 'storage_limit': '100Gi',
            'ephemeral_storage_request': '32Gi', 'ephemeral_storage_limit': '400Gi',
            'pvc_limit': '12'})

    def test_safe_cpu_decrease_still_keeps_the_cluster_pvc(self):
        plan = self._plan(_capable(_hard('30', '8Gi', '100Gi', pvc='12')),
                          target=_target('12', '8Gi', '100Gi'))
        self.assertEqual(plan['payload']['cpu_limit'], '12')
        self.assertEqual(plan['payload']['pvc_limit'], '12')
        self.assertEqual(plan['lowering_unsafe'], [])
        self.assertEqual(sorted(key for key, *_rest in plan['lowered']),
                         ['limits.cpu', 'requests.cpu'])

    def test_baseline_is_added_only_for_a_capable_operator_and_never_overrides(self):
        target = _target('8', '8Gi', '100Gi')
        self.assertNotIn('pvc_limit', with_pvc_baseline(target, _managed(_hard('8', '8Gi', '100Gi'))))
        self.assertEqual(with_pvc_baseline(target, _capable({}))['pvc_limit'], '10')
        self.assertNotIn('pvc_limit', target, '不改呼叫端的 dict')
        self.assertEqual(with_pvc_baseline(dict(target, pvc_limit='14'), _capable({}))['pvc_limit'],
                         '14')
        self.assertNotIn('pvc_limit', with_pvc_baseline(target, None))

    def test_never_lower_merge_without_baseline_is_unchanged(self):
        """沒有 pvc_limit 的目標（舊 operator 或舊呼叫端）：與 #1781 之前相同。"""
        payload, lowering, _pending = merge_never_lower(
            _target('8', '8Gi', '100Gi'), _hard('8', '8Gi', '100Gi', pvc='12'), {})
        self.assertNotIn('pvc_limit', payload)
        self.assertEqual(lowering, ['persistentvolumeclaims'])
        self.assertEqual(build_hard(payload)['persistentvolumeclaims'], '10')
