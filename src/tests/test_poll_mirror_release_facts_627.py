# -*- coding: utf-8 -*-
"""#627 (ii)：cron 走的 poll 鏡像不回寫 `helm_chart_version`。

## 缺陷

`#552` 層① 的第五條帳本回寫路徑是 poll。但 poll **有兩份實作**：

| 實作 | 呼叫者 | 原本有沒有回寫 chart_version |
| --- | --- | --- |
| `PaasController._update_service_status`（`controllers/paas.py`） | 租戶打 list／detail 端點 | ✅ 有（`**release_facts`） |
| `CloudService._poll_and_update_status`（本模組） | **cron**（`_cron_check_deploying_services`） | ❌ 四個 write 分支全部只寫 `helm_revision` |

⇒ 第五條回寫路徑實際上要求「服務在 transitional state」**且**「同時有人正在打
端點」。**唯一的無人值守觸發器（cron），回寫是缺的。**

## 為什麼這一份測試必須驅動 model 而不是 controller

`test_chart_pin_fail_closed_552.py` 已經有一支叫
`test_failed_release_branch_also_records_it` 的測試，docstring 寫「`release_facts`
必須到得了**每個** write 分支」——但**整份檔案驅動的都是 controller**
（`PaasController()._update_service_status`）。

⇒ models 鏡像整份沒有回寫，那些測試照樣全綠。**同一個功能有兩份實作時，覆蓋率
要問的是「每一份都被驅動了嗎」，不是「這一份的每個分支都被驅動了嗎」。**

本檔只驅動 `CloudService._poll_and_update_status`，**刻意不碰 controller**——
controller 那份由 `test_chart_pin_fail_closed_552.py` 守，兩份各有各的守衛才不會
再退化成「守住一份、以為守住全部」。

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
四支分支測試與下面的雙實作比對測試原本都只斷言另外兩個欄位——保護它的只剩
repo-lint 的 AST 形態判準（`_RELEASE_FACT_KEYS` 有列到，但那只守「有沒有經由
producer」，不守「值真的被蓋章了」）。四支分支測試現在各自把 baseline 設成前一天
的時間戳，斷言 poll 後有**前進**；雙實作比對測試改用「有沒有前進」而不是「兩份
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


class TestBothPollImplementationsWriteTheSameFields(_Base):
    """🔑 #627 的根因不是「漏了一個欄位」，是「兩份實作各寫一次」。

    上面四支測試守住「models 鏡像今天有回寫」。這一支守的是**結構**：兩份實作
    寫進帳本的 release 事實欄位集合必須相同。日後任何一份加了第三個欄位而另一份
    沒加，這支就紅——那正是 #627 當初沒有人發現的那一步。

    ⚠ 這是行為層斷言（實際跑兩份實作、比對寫進去的值），不是 AST／字串比對：
    形態層的判準會被「改了寫法但語意不變」誤殺，也會被「換個名字繞過」漏放。

    #642 §4.1 ④：欄位集合**不再手抄**成 `_RELEASE_FACT_FIELDS`。原本那份硬編
    清單只有兩個欄位，`_release_facts` 新增 `last_release_sync_at`（#627 (i)）
    時沒有人同步更新它，於是這支測試對第三個欄位零鑑別力——docstring 寫的
    「新增欄位時把它加進來」本身就是會漂的手抄步驟（同一個 commit 10fe7cdc 有
    同步更新 `repo-lint.py` 的 `_RELEASE_FACT_KEYS`，卻漏了這裡：兩份手抄清單，
    更新了一份、漏了一份）。現在欄位集合直接呼叫 producer 取得；per-field 比較
    器則依欄位的 Odoo 型別結構性選擇（datetime 比「有沒有前進」，其餘比「兩份
    是否相等」），不是逐名手動分類。

    ⚠ 但「從 producer 推導」單獨做，會換一個方向相反的洞：推導只認得「新增」，
    對「有人把某欄位從 `_release_facts` 拿掉」是**零鑑別力**——推導出的集合會
    自動縮回去，跟著縮的還有 repo-lint 判準 2（release 事實欄位不得當字面鍵
    出現），因為刪掉之後根本沒人再寫那個鍵。本質上：從被測物推導期望值，等於
    讓被測物自己定義及格線。`_REQUIRED_RELEASE_FACT_FIELDS` 就是擋這個反方向
    的 floor——具名而非數量下限，因為數量下限在正當重構（例如兩個欄位合併成
    一個）時會誤殺，也不表達「不能少的是哪一個」。
    """

    #: 三個寫入者（兩份 poll ＋ #627 (i) 對帳 cron）共同契約的下限。stamp 是
    #: 這個契約的核心，不是可選欄位——一旦被拿掉，這條 floor 必須紅。
    _REQUIRED_RELEASE_FACT_FIELDS = frozenset({
        'helm_revision', 'helm_chart_version', 'last_release_sync_at',
    })

    def _drive_controller(self, service, status_payload):
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController

        client = MagicMock()
        client.get_status.return_value = status_payload
        fake_request = MagicMock()
        fake_request.env = self.env
        ctrl_path = 'odoo.addons.woow_paas_platform.controllers.paas'
        with patch(f'{ctrl_path}.get_paas_operator_client', return_value=client), \
                patch(f'{ctrl_path}.request', new=fake_request):
            # #1257：controller 的 poll 只推進 state、不 dispatch init，不必再 patch。
            PaasController()._update_service_status(service)
        service.invalidate_recordset()

    def test_two_implementations_agree_on_every_release_fact(self):
        stale_sync_at = _stale_sync_at()
        stale = {
            'helm_chart_version': '0.1.0',
            'helm_revision': 2,
            'last_release_sync_at': stale_sync_at,
        }

        tpl_model = self._template('parity-model-627')
        svc_model = self._service(tpl_model, state='deploying')
        svc_model.write(dict(stale))
        self._poll(svc_model, self._status())

        tpl_ctrl = self._template('parity-ctrl-627')
        svc_ctrl = self._service(tpl_ctrl, state='deploying')
        svc_ctrl.write(dict(stale))
        self._drive_controller(svc_ctrl, self._status())

        self.assertEqual(
            svc_model.state, 'running', '前提：models 鏡像真的走完了')
        self.assertEqual(
            svc_ctrl.state, 'running', '前提：controller 那份真的走完了')

        # 欄位集合從 producer 推導，不維護第二份硬編清單——這正是 #642 §4.1 ④
        # 要收斂的洞：手抄清單漂掉時這支測試對新欄位零鑑別力。
        release_fact_fields = set(svc_model._release_facts({}).keys())

        # floor 折進迴圈之前先擋反方向的洞：純推導對「欄位被拿掉」零鑑別力
        # （集合會自動縮回去，逐名相等／前進迴圈也跟著只剩縮小後的範圍）。
        self.assertGreaterEqual(
            release_fact_fields, self._REQUIRED_RELEASE_FACT_FIELDS,
            '推導出的欄位集合掉到必要 floor 之下——這正是三個寫入者共同契約'
            '被整個拿掉時最該抓到的回歸；只靠推導等於讓被測物自己定義及格線')

        for field in sorted(release_fact_fields):
            if svc_model._fields[field].type == 'datetime':
                # 時間戳不比「兩份是否相等」：各自呼叫 fields.Datetime.now()
                # 落在不同秒是常態，硬要求相等會種下 flaky。真正的判準是兩份
                # 都必須把值從基準線往前推。
                #
                # ⚠ 這個型別分派假設「日後任何新增的 datetime 型 release 事實
                # 欄位都是同一種 stamp 語意」（基準線設在 stale_sync_at 之前）。
                # 若未來加的是別種 datetime 欄位（不是「最後一次問過」語意），
                # `stale` 字典沒有幫它種對應基準線，這裡會用 stale_sync_at 去比
                # 一個從未被種過基準線的欄位——大概率 TypeError（None 與
                # datetime 比較）而不是靜默通過。方向正確（fail-loud 好過靜默
                # 放行），但踩到時看這裡：需要幫新欄位也種一份對應基準線，或把
                # 判準從「型別」細化成「欄位名單」。
                self.assertGreater(
                    svc_model[field], stale_sync_at,
                    'models 鏡像沒有把 `%s` 從基準線前進' % field)
                self.assertGreater(
                    svc_ctrl[field], stale_sync_at,
                    'controller 那份沒有把 `%s` 從基準線前進' % field)
            else:
                self.assertEqual(
                    svc_model[field], svc_ctrl[field],
                    '兩份 poll 實作對 `%s` 的回寫結果不一致——#627 就是這樣發生'
                    '的：controller 那份寫了、cron 那份沒寫，而只驅動'
                    ' controller 的測試全綠' % field)

        self.assertNotEqual(
            svc_model['helm_chart_version'], stale['helm_chart_version'],
            '陽性對照：若兩份都沒回寫，上面的相等斷言會恆真而零鑑別力')
