"""#859：遠端刪除與本地 bookkeeping 必須是兩個失敗域。

operator 全部 mock，ORM／savepoint 由真正 Odoo TransactionCase 執行。
例外後的 DB 斷言用裸 try/except，不讓 assertRaises 的 savepoint 代替產品回捲。
"""
import logging
from unittest.mock import MagicMock, patch

from psycopg2 import OperationalError
from psycopg2.errors import SerializationFailure

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger

from ..models.smart_home import _IDENTITY_ANCHOR_FIELDS, _is_internal_identity_write
from ..services.paas_operator import (
    PaaSOperatorAPIError,
    PaaSOperatorConnectionError,
    PaaSOperatorTimeoutError,
)
from .smart_home_fixture import sh_model

_LOGGER = 'odoo.addons.woow_paas_platform.models.smart_home._logger'
_CLIENT = 'odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client'
_logger = logging.getLogger(__name__)


@tagged('post_install', '-at_install')
class TestCleanupTunnel859(TransactionCase):
    """鎖住提交點之後的事實、原始 DB 例外與提交點之前的重試契約。"""

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Cleanup 859',
        })
        self.Model = type(self.env['woow_paas_platform.smart_home'])

    def _home(self):
        return sh_model(self.env).create({
            'name': 'Cleanup tunnel 859',
            'workspace_id': self.workspace.id,
            'tunnel_id': 'tunnel-859',
            'tunnel_name': 'name-859',
            'tunnel_token': 'token-859',
            'tunnel_status': 'connected',
            'state': 'active',
        }).with_env(self.env)

    def _assert_anchors_kept(self, home):
        home.invalidate_recordset()
        self.assertTrue(home.exists())
        self.assertEqual(home.tunnel_id, 'tunnel-859')
        self.assertEqual(home.tunnel_name, 'name-859')
        self.assertEqual(home.tunnel_token, 'token-859')
        self.assertEqual(home.tunnel_status, 'connected')

    def _bookkeeping_probe(self, error, entry='_cleanup_tunnel', partial=False):
        """同一個探針供回歸與吞例外突變共用，驗注入確實在 delete 之後。"""
        home = self._home()
        client = MagicMock()
        original_write = self.Model.write
        injected = MagicMock()

        def failing_write(records, vals):
            if 'tunnel_id' not in vals:
                return original_write(records, vals)
            injected()
            client.delete_tunnel.assert_called_once_with(
                'tunnel-859', tunnel_name='name-859')
            # TransactionCase fixture 本來就是 su；這只是 bookkeeping 的必要前提，
            # 不證明產品有呼叫 sudo。提權回歸依既有 #1294 的真 tenant HTTP／RPC 測試。
            self.assertTrue(records.env.su, 'bookkeeping 必要前提：進入時 env.su=True')
            self.assertTrue(_is_internal_identity_write(records.env))
            self.assertEqual(vals, dict(
                {field: False for field in _IDENTITY_ANCHOR_FIELDS},
                tunnel_status='deleted',
            ))
            if partial:
                original_write(records, vals)
                records.flush_recordset()
            raise error

        with patch(_CLIENT, return_value=client), patch.object(
                self.Model, 'write', failing_write), patch(_LOGGER) as log:
            try:
                getattr(home, entry)()
            except UserError as caught:
                injected.assert_called_once_with()
                client.delete_tunnel.assert_called_once_with(
                    'tunnel-859', tunnel_name='name-859')
                self.assertNotIn('still live and billed', str(caught))
                self.assertIn('deleted in Cloudflare', str(caught))
                self.assertIn('local identity anchors', str(caught))
                self.assertNotIn(str(error), str(caught))
                self.assertIs(caught.__cause__, error)
                if isinstance(error, AccessError):
                    self.assertIn('permission error', str(caught))
                else:
                    self.assertNotIn('permission error', str(caught))
            else:
                self.fail('delete 已成功，但 bookkeeping 失敗被吞掉')
            injected.assert_called_once_with()
            log.exception.assert_called_once()
            self.assertIn('deleted in Cloudflare', log.exception.call_args.args[0])
            self.assertIn('local identity anchors', log.exception.call_args.args[0])
            log.warning.assert_not_called()
        self._assert_anchors_kept(home)

    def test_access_error_after_delete_is_bookkeeping_failure(self):
        self._bookkeeping_probe(AccessError('sensitive ACL detail 859'))

    def test_other_errors_after_delete_are_sanitized_and_chained(self):
        for error in (RuntimeError('sensitive internal detail 859'),
                      ValidationError('sensitive constraint detail 859')):
            with self.subTest(error=type(error).__name__):
                self._bookkeeping_probe(error)

    def test_unlink_never_claims_deleted_tunnel_is_live(self):
        for error in (AccessError('ACL unlink 859'), RuntimeError('write unlink 859')):
            with self.subTest(error=type(error).__name__):
                self._bookkeeping_probe(error, entry='unlink')

    def test_cron_cleanup_never_claims_deleted_tunnel_is_live(self):
        self._bookkeeping_probe(RuntimeError('cron write 859'), entry='_do_cleanup')

    def test_partial_bookkeeping_write_is_rolled_back(self):
        self._bookkeeping_probe(RuntimeError('partial write 859'), partial=True)

    def test_operational_error_keeps_original_exception_for_retrying(self):
        """只驗合成例外與成功回捲，不模擬真斷線或真 40001。"""
        for error in (OperationalError('DB disconnected 859'),
                      SerializationFailure('concurrent write 859')):
            with self.subTest(error=type(error).__name__):
                home = self._home()
                client = MagicMock()
                with patch(_CLIENT, return_value=client), patch.object(
                        self.Model, 'write', side_effect=error) as write, patch(
                        _LOGGER) as log:
                    try:
                        home._cleanup_tunnel()
                    except OperationalError as caught:
                        self.assertIs(caught, error)
                    else:
                        self.fail('DB OperationalError 必須保留給 Odoo retrying')
                    client.delete_tunnel.assert_called_once_with(
                        'tunnel-859', tunnel_name='name-859')
                    write.assert_called_once()
                    log.exception.assert_called_once()
                    self.assertIn('deleted in Cloudflare', log.exception.call_args.args[0])
                    log.warning.assert_not_called()
                self._assert_anchors_kept(home)

    @mute_logger('odoo.sql_db')
    def test_sql_failure_recovers_cursor_and_preserves_anchors(self):
        home = self._home()
        client = MagicMock()

        def sql_failure(records, vals):
            client.delete_tunnel.assert_called_once()
            records.env.cr.execute('SELECT 1 / 0')

        with patch(_CLIENT, return_value=client), patch.object(
                self.Model, 'write', sql_failure), patch(_LOGGER):
            try:
                home._cleanup_tunnel()
            except UserError as caught:
                self.assertIn('deleted in Cloudflare', str(caught))
                self.assertEqual(type(caught.__cause__).__name__, 'DivisionByZero')
            else:
                self.fail('SQL failure 不得吞成遠端刪除待重試')
        # 若產品沒有回捲自己的 savepoint，這裡會是真 InFailedSqlTransaction。
        self.env.cr.execute('SELECT 1')
        self.assertEqual(self.env.cr.fetchone(), (1,))
        self._assert_anchors_kept(home)

    def test_operator_failures_still_keep_anchors_for_retry(self):
        for error in (PaaSOperatorConnectionError('connection refused 859'),
                      PaaSOperatorTimeoutError('unknown remote outcome 859'),
                      PaaSOperatorAPIError('upstream 503 859', status_code=503),
                      RuntimeError('unexpected operator failure 859')):
            with self.subTest(error=type(error).__name__):
                home = self._home()
                client = MagicMock()
                client.delete_tunnel.side_effect = error
                with patch(_CLIENT, return_value=client), patch.object(
                        self.Model, 'write') as write, patch(_LOGGER) as log:
                    home._cleanup_tunnel()
                    client.delete_tunnel.assert_called_once_with(
                        'tunnel-859', tunnel_name='name-859')
                    write.assert_not_called()
                    log.warning.assert_called_once()
                    self.assertIn('kept for retry', log.warning.call_args.args[0])
                    log.exception.assert_not_called()
                self._assert_anchors_kept(home)

    def test_client_acquisition_failure_still_keeps_anchors(self):
        home = self._home()
        with patch(_CLIENT, side_effect=UserError('operator config 859')), patch(
                _LOGGER) as log:
            home._cleanup_tunnel()
            log.warning.assert_called_once()
            self.assertIn('kept for retry', log.warning.call_args.args[0])
        self._assert_anchors_kept(home)

    def test_unlink_operator_failure_still_preserves_retry_record(self):
        home = self._home()
        client = MagicMock()
        client.delete_tunnel.side_effect = PaaSOperatorConnectionError('offline 859')
        with patch(_CLIENT, return_value=client):
            try:
                home.unlink()
            except UserError as caught:
                self.assertIn('still live and billed', str(caught))
            else:
                self.fail('遠端刪不掉時不可刪除重試列')
        self._assert_anchors_kept(home)

    def test_success_clears_all_anchors_and_status(self):
        home = self._home()
        with patch(_CLIENT, return_value=MagicMock()) as factory:
            home._cleanup_tunnel()
            factory.return_value.delete_tunnel.assert_called_once_with(
                'tunnel-859', tunnel_name='name-859')
        home.invalidate_recordset()
        for field in _IDENTITY_ANCHOR_FIELDS:
            self.assertFalse(home[field])
        self.assertEqual(home.tunnel_status, 'deleted')

    def test_no_tunnel_does_not_contact_operator(self):
        home = sh_model(self.env).create({
            'name': 'No tunnel 859', 'workspace_id': self.workspace.id,
        }).with_env(self.env)
        with patch(_CLIENT) as factory:
            home._cleanup_tunnel()
            factory.assert_not_called()

    def test_positive_mutation_detects_swallowed_bookkeeping(self):
        """只突變例外吞掉的語意；同一 unlink 探針必須真的變紅。"""
        original = self.Model._cleanup_tunnel

        def swallowed_cleanup(records):
            try:
                return original(records)
            except Exception:
                # 還原舊失敗域：遠端成功後的本地例外也靜默回傳。
                return None

        with patch.object(self.Model, '_cleanup_tunnel', swallowed_cleanup):
            try:
                self._bookkeeping_probe(RuntimeError('mutation write 859'), entry='unlink')
            except AssertionError as caught:
                self.assertIn('still live and billed', str(caught))
                _logger.warning('[859] 陽性突變成立：同一探針捕獲 still live and billed 誤報')
            else:
                self.fail('突變後探針仍綠，測試沒有鑑別力')
