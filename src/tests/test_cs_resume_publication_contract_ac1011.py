"""PROPOSED native ORM regressions for separate resume-status candidate.
Not executed in this round; do not include in native47 pass counts.
"""
from unittest.mock import patch
from odoo.tests.common import TransactionCase,tagged
from ..models.cloud_service import _cs_skip_access_sync,_internal_cs_access_identity
from .test_cs_sh_fail_closed_ac1011 import Fixture

@tagged('post_install','-at_install')
class TestCsResumePublicationContractAc1011(Fixture,TransactionCase):
    def setUp(self):
        super().setUp();self.install_fake();self.resources()
    def assert_bad_resume_preserves_pending(self,protected):
        _cs_skip_access_sync(self.cs).is_protected=protected
        for body in ({},{'status':'disabled'},{'status':'error'}):
            _internal_cs_access_identity(self.cs).access_publish_pending=True
            self.cs.write({'state':'suspended','pre_suspend_state':'running'})
            with patch.object(self.client,'unpark_release_route',return_value=body):
                self.cs._do_resume()
            self.assertEqual(self.cs.state,'error')
            self.assertTrue(self.cs.access_publish_pending)
            self.assertEqual(self.cs.pre_suspend_state,'running')
    def test_protected_pending_resume_rejects_unconfirmed_2xx(self):
        self.assert_bad_resume_preserves_pending(True)
    def test_public_pending_resume_rejects_unconfirmed_2xx(self):
        self.assert_bad_resume_preserves_pending(False)
    def test_public_legacy_resume_keeps_disabled_compatibility(self):
        _cs_skip_access_sync(self.cs).is_protected=False
        self.cs.write({'state':'suspended','pre_suspend_state':'running'})
        with patch.object(self.client,'unpark_release_route',return_value={'status':'disabled'}):
            self.cs._do_resume()
        self.assertEqual(self.cs.state,'running')
        self.assertFalse(self.cs.pre_suspend_state)
        self.assertFalse(self.cs.access_publish_pending)
