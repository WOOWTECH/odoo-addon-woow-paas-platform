# -*- coding: utf-8 -*-
"""#627 (i)：對帳 cron —— 抓「非由 Odoo 發起」的 chart 變動。

## 缺陷

#552 層① 的第五條回寫路徑是 poll，設計目的就是抓「有人直接對叢集下 `helm upgrade`、
Odoo 完全不知道」。但 poll 的兩個入口與 `_cron_check_deploying_services` 的 domain
**都只收轉移狀態**——而那種變動發生時服務正是 `running` ⇒ **要抓的場景與能執行的
場景不相交**。stg `svc-bd9d6ed3` 因此漂移超過 3 天無人發現。

`_cron_reconcile_release_facts` 補上這條，**每日一次、只看 running、絕不碰 state**。

## 本檔守的四件事

1. **會回寫**：running 服務的叢集實況與帳本不一致時，帳本被修正。
2. **絕不碰 state**：這是該 cron 的核心安全性質。對 running 服務推狀態機，叢集一時
   不健康就會把租戶服務打成 `error`，而 error 沒有任何路徑會自己 poll 回來。
   ⇒ 連餵 `failed` 的 release status 都不准改 state（那正是 poll 會做的事）。
3. **範圍正確**：非 running 的服務不被本 cron 碰（那是另一條 cron 的地盤）。
4. **log 的鑑別力**：改 0 筆也要印、要印掃描範圍、漂移要點名是哪一筆與 old→new。
   缺其中任何一項，都會讓兩種不同的世界在日誌上長得一樣：
   - 不印 → 「一直沒漂移」與「cron 根本沒跑」相同
   - 只印筆數不印範圍 → 「範圍外沒漂移」與「範圍外根本沒被看」相同
   - 只印筆數不點名 → 拿不到判斷漂移頻率與幅度的母體資料

## E：`last_release_sync_at`

語意是「最後一次**問過** operator」，不是「最後一次值有變」——所以無漂移時仍然
stamp。租戶要知道的是眼前這個版本號多新。
"""
import json
import logging
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase
from ..models.cloud_service import _internal_provision

_MODEL_LOGGER = 'odoo.addons.woow_paas_platform.models.cloud_service'
_CLIENT_PATH = (
    'odoo.addons.woow_paas_platform.services.paas_operator'
    '.get_paas_operator_client'
)


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '627i Workspace',
        })
        # 這個 DB 裡可能已有別的測試建的服務；本檔的斷言一律限定在自己建的那些，
        # 否則 domain／計數類斷言會被鄰居污染。
        self.Service.search([]).unlink()

    def _template(self, slug, chart_version='0.1.0', **extra):
        vals = {
            'name': '627i %s' % slug,
            'slug': slug,
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/%s' % slug,
            'helm_chart_version': chart_version,
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
        }
        vals.update(extra)
        return self.Template.create(vals)

    def _service(self, template, state='running', **extra):
        vals = {
            'name': 'svc-627i-%s' % template.slug,
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': state,
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-627i-%s' % template.slug,
            'helm_values': json.dumps({'config': {'nonSensitive': {}}}),
        }
        vals.update(extra)
        return _internal_provision(self.Service).create(vals)

    def _client(self, **release_overrides):
        release = {'status': 'deployed', 'revision': 3, 'chart_version': '0.1.1'}
        release.update(release_overrides)
        client = MagicMock()
        client.get_status.return_value = {
            'release': release,
            'pods': [{'name': 'p1', 'phase': 'Running', 'ready': '1/1'}],
        }
        return client

    def _run(self, client):
        with patch(_CLIENT_PATH, return_value=client):
            self.Service._cron_reconcile_release_facts()


class TestReconcileWritesBackDrift(_Base):

    def test_running_service_drift_is_corrected(self):
        """stg `svc-bd9d6ed3` 的形態：叢集被別人 upgrade 了，四條主動路徑都沒跑。"""
        template = self._template('recon-drift')
        service = self._service(template)
        service.write({'helm_chart_version': '0.1.0', 'helm_revision': 2})

        self._run(self._client())
        service.invalidate_recordset()

        self.assertEqual(service.helm_chart_version, '0.1.1')
        self.assertEqual(service.helm_revision, 3)
        self.assertTrue(
            service.last_release_sync_at,
            'E：對帳完必須留下「最後一次問過」的時間，否則租戶無從判斷版本號多新')

    def test_no_drift_still_stamps_sync_time(self):
        """無漂移時**不改版本欄位**，但仍 stamp —— 語意是「問過」不是「有變」。"""
        template = self._template('recon-nodrift', chart_version='0.1.1')
        service = self._service(template)
        service.write({'helm_chart_version': '0.1.1', 'helm_revision': 3})

        self._run(self._client())
        service.invalidate_recordset()

        self.assertEqual(service.helm_chart_version, '0.1.1', '不該被動到')
        self.assertEqual(service.helm_revision, 3, '不該被動到')
        self.assertTrue(
            service.last_release_sync_at,
            '沒漂移也要 stamp——否則「一直沒漂移」與「從來沒被掃過」在資料上相同')

    def test_legacy_operator_without_field_keeps_the_value(self):
        template = self._template('recon-legacy')
        service = self._service(template)
        service.write({'helm_chart_version': '0.1.0', 'helm_revision': 2})

        client = self._client()
        del client.get_status.return_value['release']['chart_version']
        self._run(client)
        service.invalidate_recordset()

        self.assertEqual(service.helm_chart_version, '0.1.0',
                         '未知不得覆蓋成空')


class TestReconcileNeverTouchesState(_Base):
    """🔴 這條 cron 的核心安全性質。repo-lint 判準 5 另有形態層守衛。"""

    def test_state_unchanged_on_healthy_release(self):
        template = self._template('recon-state-ok')
        service = self._service(template)

        self._run(self._client())
        service.invalidate_recordset()

        self.assertEqual(service.state, 'running')

    def test_failed_release_does_not_flip_service_to_error(self):
        """poll 會把 `failed` 打成 error；對帳 cron **不准**。

        對 running 服務做狀態轉移，叢集一時不健康就會讓租戶的服務變 error，而
        error 沒有任何路徑會自己 poll 回來 ⇒ 需要人工介入才救得回來。
        """
        template = self._template('recon-state-failed')
        service = self._service(template)
        service.write({'helm_chart_version': '0.1.0'})

        self._run(self._client(status='failed', description='boom'))
        service.invalidate_recordset()

        self.assertEqual(
            service.state, 'running',
            '對帳 cron 讀到 failed 也只該記錄事實，不得推狀態機')
        self.assertEqual(
            service.helm_chart_version, '0.1.1',
            '前提：這一輪確實有跑到並回寫，否則上一條斷言零鑑別力')


class TestReconcileScope(_Base):

    def test_non_running_services_are_not_touched(self):
        running = self._service(self._template('recon-in'), state='running')
        running.write({'helm_chart_version': '0.1.0'})
        deploying = self._service(self._template('recon-out'), state='deploying')
        deploying.write({'helm_chart_version': '0.1.0'})

        self._run(self._client())
        running.invalidate_recordset()
        deploying.invalidate_recordset()

        self.assertEqual(
            running.helm_chart_version, '0.1.1',
            '前提：範圍內的那筆真的被處理了——否則下一條斷言退化成「什麼都沒做」')
        self.assertEqual(
            deploying.helm_chart_version, '0.1.0',
            'deploying 是另一條 cron 的地盤；兩條都碰會互相打架')
        self.assertFalse(
            deploying.last_release_sync_at,
            '連 stamp 都不該有——範圍外就是完全不碰')


class TestReconcilePerServiceIsolation(_Base):

    def test_one_failure_does_not_abort_the_batch(self):
        good = self._service(self._template('recon-good'))
        good.write({'helm_chart_version': '0.1.0'})
        bad = self._service(self._template('recon-bad'))
        bad.write({'helm_chart_version': '0.1.0'})

        client = self._client()

        def _status(namespace, release_name, include_vm=False):
            if release_name == bad.helm_release_name:
                raise RuntimeError('operator exploded')
            return {
                'release': {'status': 'deployed', 'revision': 3,
                            'chart_version': '0.1.1'},
                'pods': [{'name': 'p1', 'phase': 'Running', 'ready': '1/1'}],
            }

        client.get_status.side_effect = _status
        self._run(client)
        good.invalidate_recordset()
        bad.invalidate_recordset()

        self.assertEqual(
            good.helm_chart_version, '0.1.1',
            '一筆失敗不得中斷整批——這是陽性對照，證明「其餘」真的有被處理')
        self.assertEqual(
            bad.helm_chart_version, '0.1.0', '失敗那筆維持原狀，不寫半套')


class TestReconcileLogHasDiscriminatingPower(_Base):
    """log 是判斷「漂移多常發生」的**唯一母體資料**，它的形狀就是契約。"""

    def test_logs_even_when_nothing_drifted(self):
        template = self._template('recon-log-zero', chart_version='0.1.1')
        service = self._service(template)
        service.write({'helm_chart_version': '0.1.1', 'helm_revision': 3})

        # ⚠ 數值 logging.INFO 而非字串 'INFO'：Odoo 進程覆寫了名稱→數值表
        # （#492，repo-lint assertlogs-numeric-level）。
        with self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._run(self._client())

        summary = [line for line in captured.output
                   if 'reconcile-release-facts' in line]
        self.assertTrue(
            summary,
            '改 0 筆也要 log——否則「一直沒漂移」與「cron 根本沒跑」的日誌畫面相同')
        self.assertIn('0 筆漂移', summary[0])

    def test_log_states_the_scan_scope_not_just_the_count(self):
        """只印筆數會讓「範圍外沒漂移」與「範圍外根本沒被看」相同。"""
        self._service(self._template('recon-scope-run'), state='running')
        self._service(self._template('recon-scope-susp'), state='suspended')

        with self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._run(self._client())

        summary = [line for line in captured.output
                   if 'reconcile-release-facts' in line][0]
        self.assertIn('1 筆 running', summary)
        self.assertIn(
            'suspended', summary,
            '範圍外的狀態要被點名，否則讀者無從得知它們根本沒被掃')

    def test_drifted_services_are_named_with_old_and_new(self):
        """「是哪一個」而不是「差幾個」，而且要有 old→new。"""
        template = self._template('recon-log-name')
        service = self._service(template)
        service.write({'helm_chart_version': '0.1.0', 'helm_revision': 2})

        with self.assertLogs(_MODEL_LOGGER, level=logging.INFO) as captured:
            self._run(self._client())

        named = [line for line in captured.output
                 if 'id=%s' % service.id in line]
        self.assertTrue(named, '漂移的那筆必須被點名（id ＋ 名稱）')
        self.assertIn(service.name, named[0])
        self.assertIn(
            '0.1.0→0.1.1', named[0],
            'old→new 兩端都要有——只印新值就拿不到「漂了多遠」這份資料')
