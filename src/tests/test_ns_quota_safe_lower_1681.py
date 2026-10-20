"""#1681／#1682 共用的安全降額判斷（``services/ns_quota_policy.plan_safe_lower``）。

純函式：輸入目標 payload 與 operator ``managed_quota``，輸出可送的 payload 與每項判斷。
管理員確認流程（#1681）與生命週期自動降額（#1682）都只呼叫這一份。
"""
from odoo.tests.common import BaseCase, tagged

from ..services.ns_quota_policy import merge_never_lower, plan_safe_lower

FIVE = ('requests.cpu', 'limits.cpu', 'requests.memory', 'limits.memory', 'requests.storage')


def _hard(cpu, memory, storage, eph_req=None, eph_lim=None, pvc='10'):
    hard = {'requests.cpu': cpu, 'limits.cpu': cpu, 'requests.memory': memory,
            'limits.memory': memory, 'persistentvolumeclaims': pvc, 'requests.storage': storage}
    if eph_req:
        hard['requests.ephemeral-storage'] = eph_req
    if eph_lim:
        hard['limits.ephemeral-storage'] = eph_lim
    return hard


def _managed(hard, used=None, **extra):
    used_map = {key: '0' for key in hard}
    used_map.update(used or {})
    managed = {'available': True, 'reason': None, 'hard': dict(hard), 'spec_hard': dict(hard),
               'used': used_map, 'sync_token': 'tok', 'resource_version': '7'}
    managed.update(extra)
    return managed


def _target(cpu, memory, storage, eph_req='16Gi', eph_lim='240Gi'):
    return {'cpu_limit': cpu, 'memory_limit': memory, 'storage_limit': storage,
            'ephemeral_storage_request': eph_req, 'ephemeral_storage_limit': eph_lim}


@tagged('post_install', '-at_install')
class TestPlanSafeLower1681(BaseCase):

    def test_no_decrease_matches_never_lower_merge(self):
        managed = _managed(_hard('8', '8Gi', '100Gi', '16Gi', '240Gi'))
        target = _target('12', '8Gi', '100Gi')
        plan = plan_safe_lower(target, managed)
        payload, _lowering, _pending = merge_never_lower(
            target, managed['spec_hard'], managed['hard'])
        self.assertEqual(plan['payload'], payload)
        self.assertEqual(plan['lowered'], [])
        self.assertEqual(plan['blocked'], [])
        self.assertEqual(plan['lowering_unsafe'], [])

    def test_decrease_with_room_lowers_every_field_to_target(self):
        managed = _managed(_hard('18', '40Gi', '300Gi', '16Gi', '240Gi'),
                           used={'requests.cpu': '3', 'limits.cpu': '6',
                                 'requests.memory': '6Gi', 'limits.memory': '12Gi',
                                 'requests.storage': '80Gi'})
        plan = plan_safe_lower(_target('8', '16Gi', '100Gi'), managed)
        self.assertEqual(plan['blocked'], [])
        self.assertEqual(plan['payload']['cpu_limit'], '8')
        self.assertEqual(plan['payload']['memory_limit'], '16Gi')
        self.assertEqual(plan['payload']['storage_limit'], '100Gi')
        self.assertEqual({item[0] for item in plan['lowered']}, set(FIVE))
        lowered = {item[0]: item for item in plan['lowered']}
        self.assertEqual(lowered['limits.cpu'], ('limits.cpu', '18', '8', '6'))
        self.assertEqual(plan['pending_decrease'], [])

    def test_used_equal_to_target_is_safe(self):
        managed = _managed(_hard('18', '8Gi', '100Gi'), used={'limits.cpu': '8'})
        plan = plan_safe_lower(_target('8', '8Gi', '100Gi'), managed)
        self.assertEqual(plan['blocked'], [])
        self.assertEqual(plan['payload']['cpu_limit'], '8')

    def test_limits_used_above_target_blocks_even_when_requests_fit(self):
        # requests 只用 2，但 limits 用到 10：單看 requests 會誤判可降。
        managed = _managed(_hard('18', '8Gi', '100Gi'),
                           used={'requests.cpu': '2', 'limits.cpu': '10'})
        plan = plan_safe_lower(_target('8', '8Gi', '100Gi'), managed)
        self.assertEqual([key for key, _reason in plan['blocked']], ['limits.cpu'])
        self.assertIn('used_exceeds', plan['blocked'][0][1])
        self.assertEqual(plan['payload']['cpu_limit'], '18')
        self.assertEqual(plan['lowered'], [])

    def test_requests_used_above_target_blocks_even_when_limits_fit(self):
        managed = _managed(_hard('8', '40Gi', '100Gi'),
                           used={'requests.memory': '20Gi', 'limits.memory': '10Gi'})
        plan = plan_safe_lower(_target('8', '16Gi', '100Gi'), managed)
        self.assertEqual([key for key, _reason in plan['blocked']], ['requests.memory'])
        self.assertEqual(plan['payload']['memory_limit'], '40Gi')

    def test_one_unsafe_field_cancels_every_decrease(self):
        # cpu 可安全降，storage 不行（保留 PVC 佔著 250Gi）：全有或全無，cpu 也不降。
        managed = _managed(_hard('18', '8Gi', '300Gi'),
                           used={'limits.cpu': '2', 'requests.cpu': '2',
                                 'requests.storage': '250Gi'})
        target = _target('8', '8Gi', '100Gi')
        plan = plan_safe_lower(target, managed)
        payload, _lowering, pending = merge_never_lower(
            target, managed['spec_hard'], managed['hard'])
        self.assertEqual(plan['payload'], payload)
        self.assertEqual(plan['lowered'], [])
        self.assertEqual([key for key, _reason in plan['blocked']], ['requests.storage'])
        self.assertEqual(plan['pending_decrease'], pending)

    def test_increase_still_goes_out_when_a_decrease_is_blocked(self):
        managed = _managed(_hard('8', '40Gi', '100Gi'), used={'limits.memory': '30Gi'})
        plan = plan_safe_lower(_target('12', '16Gi', '100Gi'), managed)
        self.assertEqual(plan['payload']['cpu_limit'], '12')
        self.assertEqual(plan['payload']['memory_limit'], '40Gi')
        self.assertTrue(plan['blocked'])

    def test_missing_used_key_fails_closed_even_for_another_resource(self):
        managed = _managed(_hard('18', '8Gi', '100Gi'))
        del managed['used']['requests.storage']
        plan = plan_safe_lower(_target('8', '8Gi', '100Gi'), managed)
        self.assertEqual(plan['blocked'], [('requests.storage', 'used_missing')])
        self.assertEqual(plan['payload']['cpu_limit'], '18')

    def test_unparseable_used_fails_closed(self):
        managed = _managed(_hard('18', '8Gi', '100Gi'), used={'limits.cpu': 'lots'})
        plan = plan_safe_lower(_target('8', '8Gi', '100Gi'), managed)
        self.assertEqual(plan['blocked'], [('limits.cpu', 'used_invalid')])
        self.assertEqual(plan['payload']['cpu_limit'], '18')

    def test_missing_used_map_does_not_matter_without_a_decrease(self):
        managed = _managed(_hard('8', '8Gi', '100Gi'))
        del managed['used']
        plan = plan_safe_lower(_target('12', '8Gi', '100Gi'), managed)
        self.assertEqual(plan['blocked'], [])
        self.assertEqual(plan['payload']['cpu_limit'], '12')

    def test_old_operator_without_sync_token_fails_closed(self):
        managed = _managed(_hard('18', '8Gi', '100Gi'))
        del managed['sync_token']
        plan = plan_safe_lower(_target('8', '8Gi', '100Gi'), managed)
        self.assertEqual(plan['blocked'], [('*', 'operator_unsupported')])
        self.assertEqual(plan['payload']['cpu_limit'], '18')
        self.assertEqual(plan['lowered'], [])

    def test_unavailable_or_absent_managed_quota_has_no_payload(self):
        for managed, reason in (
                (None, 'operator_unsupported'),
                ({'available': False, 'reason': 'quota_unreadable'},
                 'managed_unavailable: quota_unreadable'),
                ({'available': False, 'reason': 'managed_quota_incomplete', 'sync_token': None},
                 'managed_unavailable: managed_quota_incomplete')):
            plan = plan_safe_lower(_target('8', '8Gi', '100Gi'), managed)
            self.assertIsNone(plan['payload'])
            self.assertEqual(plan['blocked'], [('*', reason)])
            self.assertEqual(plan['lowered'], [])

    def test_cluster_value_is_the_larger_of_spec_and_status(self):
        # spec 已降到 8 但 status 還停在 18：仍視為降額，要檢查 used。
        managed = _managed(_hard('8', '8Gi', '100Gi'), used={'limits.cpu': '12'})
        managed['hard'] = _hard('18', '8Gi', '100Gi')
        plan = plan_safe_lower(_target('8', '8Gi', '100Gi'), managed)
        self.assertEqual([key for key, _reason in plan['blocked']], ['limits.cpu'])

    def test_ephemeral_storage_and_pvc_are_never_lowered(self):
        managed = _managed(_hard('8', '8Gi', '100Gi', '32Gi', '400Gi'))
        plan = plan_safe_lower(_target('8', '8Gi', '100Gi', '16Gi', '240Gi'), managed)
        self.assertEqual(plan['blocked'], [])
        self.assertEqual(plan['payload']['ephemeral_storage_request'], '32Gi')
        self.assertEqual(plan['payload']['ephemeral_storage_limit'], '400Gi')
        self.assertIn(('requests.ephemeral-storage', '32Gi', '16Gi'), plan['pending_decrease'])
        self.assertEqual(plan['lowered'], [])

    def test_pvc_above_operator_constant_is_reported_as_unsafe_lowering(self):
        managed = _managed(_hard('18', '8Gi', '100Gi', pvc='12'))
        plan = plan_safe_lower(_target('8', '8Gi', '100Gi'), managed)
        self.assertEqual(plan['lowering_unsafe'], ['persistentvolumeclaims'])

    def test_unparseable_cluster_hard_fails_closed(self):
        managed = _managed(_hard('18', '8Gi', '100Gi'))
        managed['hard']['limits.cpu'] = 'weird'
        plan = plan_safe_lower(_target('8', '8Gi', '100Gi'), managed)
        self.assertIsNone(plan['payload'])
        self.assertEqual(plan['blocked'][0][0], '*')
        self.assertTrue(plan['blocked'][0][1].startswith('quota_not_provable'))
