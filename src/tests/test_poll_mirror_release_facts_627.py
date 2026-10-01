# -*- coding: utf-8 -*-
"""#627 (ii)：cron 走的 poll 鏡像不回寫 `helm_chart_version`。

## 缺陷

`#552` 層① 的第五條帳本回寫路徑是 poll。但 poll 當時**有兩份實作**（#1159 起
list/get 純讀，controller 那份已刪除，只剩 cron 那份）：

| 實作 | 呼叫者 | 原本有沒有回寫 chart_version |
| --- | --- | --- |
| `PaasController._update_service_status`（`controllers/paas.py`） | 租戶打 list／detail 端點 | ✅ 有（`**release_facts`） |
| `CloudService._poll_and_update_status`（本模組） | **cron**（`_cron_check_deploying_services`） | ❌ 四個 write 分支全部只寫 `helm_revision` |

⇒ 第五條回寫路徑實際上要求「服務在 transitional state」**且**「同時有人正在打
端點」。**唯一的無人值守觸發器（cron），回寫是缺的。**

## 為什麼這一份測試必須驅動 model 而不是 controller

（歷史）`test_chart_pin_fail_closed_552.py` 當時已經有一支叫
`test_failed_release_branch_also_records_it` 的測試，docstring 寫「`release_facts`
必須到得了**每個** write 分支」——但**整份檔案驅動的都是 controller**
（`PaasController()._update_service_status`）。

⇒ models 鏡像整份沒有回寫，那些測試照樣全綠。**同一個功能有兩份實作時，覆蓋率
要問的是「每一份都被驅動了嗎」，不是「這一份的每個分支都被驅動了嗎」。**

#1159 起 list/get 純讀、controller 那份 poll 已刪除，`_poll_and_update_status` 是唯一
的 poll 實作；`test_chart_pin_fail_closed_552.py` 也已改為驅動它。本檔仍逐一驅動
model poll 的每個 write 分支。

## 四個 write 分支

鏡像有四個 `service.write(...)`，本檔逐一驅動並各自帶前提斷言（證明真的走到那條
分支，而不是在「什麼都沒發生」的狀態下對回寫下斷言）：

| 分支 | 觸發條件 | 前提斷言 |
| --- | --- | --- |
| → `initializing`（首裝） | 需要 init ＋ 原狀態 `deploying` | `state == 'initializing'` |
| → `initializing`（升級後補 init） | 需要 init ＋ 原狀態 `upgrading` ＋ `addon_update_pending` | `state == 'initializing'` |
| → `running` | 不需要 init | `state == 'running'` |
| → `error` | release status `failed` | `state == 'error'` |

⚠ 第五個分支（需要 init ＋ 原狀態 `initializing`）**沒有 write**，只呼叫
post-deploy init，故不在本表——它沒有可回寫的位置，不是漏測。

## #642 §4.1 ④：補 `last_release_sync_at` 的行為層覆蓋

`_release_facts` 的 `last_release_sync_at` 是無條件 `fields.Datetime.now()`（不像
`helm_revision`／`helm_chart_version` 有 `release.get(...)` 的 fallback），但上面
四支分支測試與下面的欄位完整性測試原本都只斷言另外兩個欄位——保護它的只剩
repo-lint 的 AST 形態判準（`_RELEASE_FACT_KEYS` 有列到，但那只守「有沒有經由
producer」，不守「值真的被蓋章了」）。四支分支測試現在各自把 baseline 設成前一天
的時間戳，斷言 poll 後有**前進**；欄位完整性測試同樣用「有沒有前進」而不是「兩份
相等」——兩次呼叫 `fields.Datetime.now()` 落在不同秒是常態，硬要求相等會種下
flaky。
"""
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase
from ..models.cloud_service import _internal_provision

_MODEL_PATH = 'odoo.addons.woow_paas_platform.models.cloud_service.CloudService'


def _stale_sync_at():
    """`last_release_sync_at` 的基準線：前一天，讓「有沒有前進」有鑑別力。

    不用 `False`——`False` 只能斷言「truthy」，蓋章值凍結在某個非 False
    的舊值時 truthy 斷言仍會誤放；「前進」才是這個欄位的真實語意（見
    `_release_facts` docstring：語意是「最後一次問過」不是「最後一次值有
    變」）。

    模組層級函式而非 `_Base` 方法：`test_chart_pin_fail_closed_552.py`
    守 controller 那份 poll 的四條 write 分支，需要同一個基準線邏輯，但
    它有自己的 `_Base`（不同的 `_template`／`_service` fixture）。手抄
    第二份是這次要收斂的那個洞的同形——改成模組層級函式讓它直接
    `from .test_poll_mirror_release_facts_627 import _stale_sync_at`，
    與本檔的既有慣例一致（見 `test_portal_agent_persist.py` 匯入
    `test_portal_agent_page.PortalAgentFixture` 的同型寫法）。
    """
    return fields.Datetime.now() - timedelta(days=1)


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '627 Workspace',
        })

    def _template(self, slug, chart_version='0.1.0', **extra):
        vals = {
            'name': '627 %s' % slug,
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

    def _service(self, template, state='deploying', **extra):
        vals = {
            'name': 'svc-627-%s' % template.slug,
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': state,
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-627-%s' % template.slug,
            'helm_values': json.dumps({'config': {'nonSensitive': {}}}),
        }
        vals.update(extra)
        return _internal_provision(self.Service).create(vals)

    def _status(self, **release_overrides):
        """operator `get_status` 的回應。

        ⚠ `pods` 不可留空：`_is_container_ready` 對空陣列是 fail-closed
        （#456／F-423-1），不就緒就整段不會 write ⇒ 測試會在「什麼都沒發生」的
        狀態下對回寫下斷言。每條測試的前提斷言就是擋這個的。
        """
        release = {'status': 'deployed', 'revision': 3, 'chart_version': '0.1.1'}
        release.update(release_overrides)
        return {
            'release': release,
            'pods': [{'name': 'p1', 'phase': 'Running', 'ready': '1/1'}],
        }

    def _poll(self, service, status_payload):
        """驅動 **models 鏡像**（cron 走的那一份），不是 controller。

        post-deploy init 一律 patch 掉：它會真的去打 operator 的 init 端點，
        與本檔要驗的回寫無關，且會在兩條 `initializing` 分支上製造雜訊。
        """
        client = MagicMock()
        client.get_status.return_value = status_payload
        with patch(f'{_MODEL_PATH}._run_post_deploy_init', return_value=None):
            service._poll_and_update_status(client)
        service.invalidate_recordset()


class TestCronPollMirrorRecordsChartVersion(_Base):
    """四個 write 分支逐一驅動——這是 cron 唯一會走的那份實作。"""

    def test_running_branch_records_chart_version(self):
        """→ `running`（不需要 post-deploy init）。"""
        template = self._template('poll-run-627')
        service = self._service(template, state='deploying')
        stale_sync_at = _stale_sync_at()
        service.write({
            'helm_chart_version': '0.1.0',
            'helm_revision': 2,
            'last_release_sync_at': stale_sync_at,
        })

        self._poll(service, self._status())

        self.assertEqual(service.state, 'running', '前提：走的是 → running 分支')
        self.assertEqual(service.helm_revision, 3, '前提：這條 poll 真的寫了')
        self.assertEqual(
            service.helm_chart_version, '0.1.1',
            'cron 是唯一的無人值守觸發器；它不回寫的話，「服務在轉移狀態時 '
            'chart 被外部換掉」永遠只能靠剛好有人在看頁面才會被記錄')
        self.assertGreater(
            service.last_release_sync_at, stale_sync_at,
            '這條 write 分支必須把 last_release_sync_at 從基準線前進——語意是'
            '「最後一次問過 operator」，不是「值有沒有變」')

    def test_error_branch_records_chart_version(self):
        """→ `error`（release status `failed`）。"""
        template = self._template('poll-err-627')
        service = self._service(template, state='deploying')
        stale_sync_at = _stale_sync_at()
        service.write({
            'helm_chart_version': '0.1.0',
            'helm_revision': 2,
            'last_release_sync_at': stale_sync_at,
        })

        self._poll(service, self._status(status='failed', description='boom'))

        self.assertEqual(service.state, 'error', '前提：走的是 → error 分支')
        self.assertEqual(service.helm_revision, 3, '前提：這條 poll 真的寫了')
        self.assertEqual(service.helm_chart_version, '0.1.1')
        self.assertGreater(
            service.last_release_sync_at, stale_sync_at,
            '就算走的是 error 分支，release_facts 仍然被展開進去，'
            'last_release_sync_at 也必須前進')

    def test_initializing_branch_from_deploying_records_chart_version(self):
        """→ `initializing`（首裝：需要 init ＋ 原狀態 `deploying`）。"""
        template = self._template('poll-init-627', post_deploy_init_type='n8n')
        service = self._service(template, state='deploying')
        stale_sync_at = _stale_sync_at()
        service.write({
            'helm_chart_version': '0.1.0',
            'helm_revision': 2,
            'last_release_sync_at': stale_sync_at,
        })

        self._poll(service, self._status())

        self.assertEqual(
            service.state, 'initializing', '前提：走的是 → initializing（首裝）分支')
        self.assertEqual(service.helm_revision, 3, '前提：這條 poll 真的寫了')
        self.assertEqual(service.helm_chart_version, '0.1.1')
        self.assertGreater(
            service.last_release_sync_at, stale_sync_at,
            '首裝分支同樣必須把 last_release_sync_at 從基準線前進')

    def test_initializing_branch_from_upgrading_records_chart_version(self):
        """→ `initializing`（升級後補 init：`upgrading` ＋ `addon_update_pending`）。

        這條分支正是「chart 剛被換過」最可能發生的時刻，漏掉它等於在最該記錄的
        那一刻不記錄。
        """
        template = self._template('poll-upg-627', post_deploy_init_type='n8n')
        service = self._service(template, state='upgrading')
        stale_sync_at = _stale_sync_at()
        service.write({
            'helm_chart_version': '0.1.0',
            'helm_revision': 2,
            'addon_update_pending': True,
            'last_release_sync_at': stale_sync_at,
        })

        self._poll(service, self._status())

        self.assertEqual(
            service.state, 'initializing', '前提：走的是 → initializing（升級）分支')
        self.assertEqual(service.helm_revision, 3, '前提：這條 poll 真的寫了')
        self.assertEqual(service.helm_chart_version, '0.1.1')
        self.assertGreater(
            service.last_release_sync_at, stale_sync_at,
            '「chart 剛被換過」最可能發生的這一刻，last_release_sync_at 也必須'
            '前進，不能只顧 chart_version')


class TestCronPollMirrorBackwardCompat(_Base):
    """舊 operator 沒有這個欄位時保留原值——`or self.helm_chart_version`。"""

    def test_operator_without_the_field_keeps_the_value(self):
        template = self._template('poll-legacy-627')
        service = self._service(template, state='deploying')
        service.write({'helm_chart_version': '0.1.0', 'helm_revision': 2})

        status = self._status()
        del status['release']['chart_version']
        self._poll(service, status)

        self.assertEqual(service.helm_revision, 3, '前提：這條 poll 真的寫了')
        self.assertEqual(
            service.helm_chart_version, '0.1.0',
            '未知不得覆蓋成空——舊 operator 不回這個欄位時要保留帳本原值')

    def test_empty_string_from_operator_keeps_the_value(self):
        """陽性對照的另一半：空字串與缺欄位都要走 fallback。

        `or` 對兩者行為相同，但那是實作細節；把它釘成契約，否則日後改成
        `'chart_version' in release` 這種寫法時，空字串會把帳本洗成空值而沒有
        任何測試會紅。
        """
        template = self._template('poll-empty-627')
        service = self._service(template, state='deploying')
        service.write({'helm_chart_version': '0.1.0', 'helm_revision': 2})

        self._poll(service, self._status(chart_version=''))

        self.assertEqual(service.helm_revision, 3, '前提：這條 poll 真的寫了')
        self.assertEqual(service.helm_chart_version, '0.1.0')


class TestPollWritesEveryReleaseFact(_Base):
    """🔑 #627 的根因不是「漏了一個欄位」，是「兩份實作各寫一次」。

    #1159 起 list/get 純讀、controller 那份 poll 已刪除（第二份不得長回來由 repo-lint
    ``poll-release-facts-single-source`` 判準 6 守）。原本這裡比對兩份實作寫進帳本的
    欄位是否相同；現在只剩一份，改守「這一份把 producer 的**每一個**欄位都寫了」。

    ⚠ 行為層斷言（實際跑 poll、看寫進去的值），不是 AST／字串比對。

    #642 §4.1 ④：欄位集合**不手抄**，直接呼叫 producer 取得；per-field 比較器依欄位
    的 Odoo 型別結構性選擇（datetime 比「有沒有前進」，其餘比「是否等於 operator 回報
    的值」）。

    ⚠ 但「從 producer 推導」單獨做，對「有人把某欄位從 `_release_facts` 拿掉」是**零
    鑑別力**——推導出的集合會自動縮回去。`_REQUIRED_RELEASE_FACT_FIELDS` 就是擋這個
    反方向的具名 floor。
    """

    #: 寫入者（poll ＋ #627 (i) 對帳 cron）共同契約的下限。stamp 是這個契約的核心，
    #: 不是可選欄位——一旦被拿掉，這條 floor 必須紅。
    _REQUIRED_RELEASE_FACT_FIELDS = frozenset({
        'helm_revision', 'helm_chart_version', 'last_release_sync_at',
    })

    def test_poll_writes_every_release_fact(self):
        stale_sync_at = _stale_sync_at()
        stale = {
            'helm_chart_version': '0.1.0',
            'helm_revision': 2,
            'last_release_sync_at': stale_sync_at,
        }
        service = self._service(self._template('facts-627'), state='deploying')
        service.write(dict(stale))
        self._poll(service, self._status())
        self.assertEqual(service.state, 'running', '前提：poll 真的走完了')

        release_fact_fields = set(service._release_facts({}).keys())
        self.assertGreaterEqual(
            release_fact_fields, self._REQUIRED_RELEASE_FACT_FIELDS,
            '推導出的欄位集合掉到必要 floor 之下——只靠推導等於讓被測物自己定義及格線')

        # operator 回報的值（見 `_Base._status`），與被測物無關的獨立來源。
        reported = {'helm_revision': 3, 'helm_chart_version': '0.1.1'}
        for field in sorted(release_fact_fields):
            if service._fields[field].type == 'datetime':
                self.assertGreater(
                    service[field], stale_sync_at,
                    'poll 沒有把 `%s` 從基準線前進' % field)
            else:
                self.assertIn(field, reported,
                              '新的 release 事實欄位 `%s`：請在本測試補上 operator 回報值'
                              % field)
                self.assertEqual(service[field], reported[field],
                                 'poll 沒有把 `%s` 寫成 operator 回報的值' % field)
