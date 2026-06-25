import json as json_lib

from odoo.tests.common import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestTopupStatusEndpoint(HttpCase):
    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].create({
            'name': 'StatusUser', 'login': 'stat', 'password': 'stat',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.plan = self.env['woow_paas_platform.topup_plan'].create({
            'name': 'S1', 'amount': 100, 'credits': 100, 'active': True,
        })
        self.topup = self.env['woow.wallet.topup'].sudo().create({
            'partner_id': self.user.partner_id.id,
            'plan_id': self.plan.id,
            'amount': 100, 'credits': 100,
            'currency_id': self.env.company.currency_id.id,
            'state': 'done',
        })
        self.authenticate('stat', 'stat')

    def _get(self, topup_id):
        response = self.url_open(
            f'/api/billing/topup/{topup_id}/status',
            data=json_lib.dumps({'params': {}}),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        return response.json()

    def test_owner_reads_status(self):
        r = self._get(self.topup.id)['result']
        self.assertTrue(r['success'])
        self.assertEqual(r['data']['state'], 'done')
        self.assertEqual(r['data']['credits'], 100)

    def test_other_user_gets_not_found(self):
        other = self.env['res.users'].create({
            'name': 'Other', 'login': 'other', 'password': 'other',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.authenticate('other', 'other')
        r = self._get(self.topup.id)['result']
        self.assertFalse(r['success'])
        self.assertEqual(r['code'], 'not_found')
