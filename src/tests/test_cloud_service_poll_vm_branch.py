"""就緒判定的 VM 分支 fail-closed 回歸測試（#423 W5a）。

**這是本次改動最重要的守線。**

背景（design §4.1 抓到的缺陷）：就緒判定原本在 model 與 controller 各有一份
完全同型的複製品，兩份都是

    all_ready = all(...) if active_pods else (not pods)

—— ``pods`` 為空時回 True。對 VM 型服務這是災難：virt-launcher pod 還沒建出來
（磁碟正在 import）時，服務就被標成 ``running``，租戶拿到一台開不了的機器。

而且**只修 model 層不夠**：租戶只要打開前台服務詳情頁，controller 那份就會把
狀態覆寫回 running。所以下面 model 與 controller 兩層都測。

W5a 的收斂做法是把判定抽成單一事實源 ``CloudService._is_release_ready``，
兩層都呼叫它。#1–#6 直接測該判定。#7–#10 原本測兩個 controller 呼叫點確實走到
同一條路；#1159 起 list/get 純讀、controller 那份 poll 已刪除，那組測試隨之移除
（第二份 poll 不得長回來由 repo-lint ``poll-release-facts-single-source`` 判準 6 守）。

**2026-07-31 更新（F-423-1 Step 1）**：上面「容器型邏輯在 pods 為空時回 True」那句
描述的是 W5a 當時的狀態；容器分支已經一併改成 fail-closed（#456 實錄兩筆「壞掉卻
顯示 Running」）。連帶地，W5a 對「純 VM 的 pods 是空的」這個假設也被實測推翻——見
下面 ``_VIRT_LAUNCHER`` 的說明。本檔因此有三類測試：VM fail-closed（W5a 原有）、
container fail-closed（Step 1 新語意）、以及 poll 的端到端鎖。
"""
from unittest.mock import MagicMock

from odoo.tests.common import TransactionCase
from ..models.cloud_service import _internal_provision
from .operator_contract import status_response  # #1052：get_status 形狀的唯一定義處


# KubeVirt 的 VM 跑在 virt-launcher pod 裡，而 haos chart 把
# `app.kubernetes.io/instance` 標在 VirtualMachine 的 pod template 上 —— 所以那顆
# pod **帶著** operator 抓 pod 用的 selector，一個 Running 的 VM 在 `get_status` 的
# `pods` 裡看得到。2026-07-31 於 woow-k3s 實測：
#
#   NAME                                    READY  STATUS   LABELS
#   virt-launcher-svc-5851cdbf-haos-7b876   2/2    Running  app.kubernetes.io/instance=svc-5851cdbf,...
#
# W5a 當時的假設相反（以為純 VM 的 `pods` 是空的，靠容器端 fail-open 才不會卡住），
# 那句話寫進了 `cloud_service.py` 的註解，也寫進了下面幾條 VM 測試的 fixture。
# F-423-1 Step 1 把容器端的空陣列改成 fail-closed 之後，這個差異就從「碰巧不影響」
# 變成「fixture 不真實就會紅」——故補上這顆 pod，讓 VM 的 fixture 反映實測。
_VIRT_LAUNCHER = [{
    'name': 'virt-launcher-svc-test-7b876', 'phase': 'Running', 'ready': '2/2',
}]


def _status(release_status='deployed', pods=None, **vm_fields):
    """組一份 operator ``get_status`` 形狀的回應。

    預設模擬「未帶 include_vm」——operator 此時回 ``vm_included: False`` 與
    ``vmis: None``（不是 ``[]``），這個區別正是 fail-closed 的依據。

    #1052：形狀的唯一定義處是 ``operator_contract.status_response``（對帳到 operator
    ``ReleaseStatusResponse``）。本檔原本手抄的 7 key 版本少了 ``pod_retrieval_error``；
    消費端一律 ``status.get('pod_retrieval_error')``（cloud_service.py 的 pod 就緒判定），
    多一個 ``None`` key 與缺 key 等價。
    """
    return status_response(release_status, pods, **vm_fields)


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'W5a Test Workspace',
        })

    def _make_service(self, workload_type, state='deploying'):
        template = self.Template.create({
            'name': f'T-{workload_type}',
            'slug': f'slug-{workload_type}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'chart',
            'helm_chart_version': '1.0.0',
            'workload_type': workload_type,
            'post_deploy_init_type': 'none',
        })
        return _internal_provision(self.Service).create({
            'name': f'svc-{workload_type}',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': state,
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-test',
        })


class TestReadinessDecision(_Base):
    """#1–#6：單一事實源本身的判定（model 層）。"""

    # ---- VM：fail-closed ------------------------------------------------

    def test_1_vm_with_no_vmis_stays_not_ready(self):
        """🔴 核心回歸：VM 型 + 沒有 VMI → **絕不可**判定就緒。

        這正是 design 抓到的 fail-open：容器型邏輯在 pods 為空時回 True，
        套到 VM 上就是「磁碟還在 import 就說開好了」。
        """
        svc = self._make_service('vm')

        ready, detail = svc._is_release_ready(
            _status(vm_included=True, vmis=[])
        )

        self.assertFalse(ready, "VM 沒有 VMI 時必須 fail-closed")
        self.assertIn('no VMI', detail)

    def test_1b_vm_with_vmis_none_stays_not_ready(self):
        """漏帶 include_vm（vmis=None、vm_included=False）同樣不可就緒。

        這是程式錯誤，但寧可服務卡住也不能誤報就緒——且 detail 會明說原因，
        讓它可診斷而不是靜默。
        """
        svc = self._make_service('vm')

        ready, detail = svc._is_release_ready(_status())

        self.assertFalse(ready)
        self.assertIn('include_vm', detail)

    def test_2_vm_running_and_ready_is_ready(self):
        svc = self._make_service('vm')

        ready, _ = svc._is_release_ready(_status(
            vm_included=True,
            vmis=[{'name': 'svc-test', 'phase': 'Running', 'ready': True}],
            pods=_VIRT_LAUNCHER,
        ))

        self.assertTrue(ready)

    def test_3_vm_running_but_not_ready_condition(self):
        """phase=Running 但 Ready condition 為 False → 未就緒。

        兩者是獨立訊號：QEMU 起來（Running）遠早於 guest 可用（Ready）。
        """
        svc = self._make_service('vm')

        ready, detail = svc._is_release_ready(_status(
            vm_included=True,
            vmis=[{'name': 'svc-test', 'phase': 'Running', 'ready': False}],
        ))

        self.assertFalse(ready)
        self.assertIn('not ready', detail)

    def test_4_vm_pending_phase_is_not_ready(self):
        svc = self._make_service('vm')

        ready, _ = svc._is_release_ready(_status(
            vm_included=True,
            vmis=[{'name': 'svc-test', 'phase': 'Pending', 'ready': False}],
        ))

        self.assertFalse(ready)

    def test_vmi_retrieval_error_is_fail_closed(self):
        """VMI 查詢失敗 → 狀態未知 → 不可就緒。"""
        svc = self._make_service('vm')

        ready, detail = svc._is_release_ready(_status(
            vm_included=True, vmis=None,
            vmi_retrieval_error='Forbidden',
        ))

        self.assertFalse(ready)
        self.assertIn('Forbidden', detail)

    def test_datavolume_error_does_not_block_a_ready_vm(self):
        """⚠ 語意分流：DataVolume 出錯**不**擋就緒。

        DataVolume 只供進度顯示。讓它擋住一台已經 Ready 的 VM，等於把
        「進度條壞了」升級成「服務不可用」。
        """
        svc = self._make_service('vm')

        ready, _ = svc._is_release_ready(_status(
            vm_included=True,
            vmis=[{'name': 'svc-test', 'phase': 'Running', 'ready': True}],
            datavolume_retrieval_error='CRD missing',
            pods=_VIRT_LAUNCHER,
        ))

        self.assertTrue(ready, "DataVolume 錯誤不該擋住已 Ready 的 VM")

    # ---- container：F-423-1 Step 1 之後同樣 fail-closed --------------------

    def test_5_container_with_no_pods_is_fail_closed(self):
        """容器型 + 無 pod → **不可就緒**（F-423-1 Step 1，2026-07-31 翻轉）。

        本條原本是 characterization（`..._keeps_current_behaviour`），釘住 W5a
        刻意保留的 fail-open：`pods` 為空回 True。翻轉理由見 F-423-1 評估——

        - **誤報已實際發生**：#456 兩筆 hermes-agent 服務因 ResourceQuota 拒絕
          pod 建立（ReplicaSet `FailedCreate`）而永久壞掉，DB / UI 卻都顯示
          Running/ONLINE。hermes-agent 的 `post_deploy_init_type='none'`，所以
          走到 `running` 的唯一路徑就是這裡回 True。
        - **誤報是黏住的**：poll 只撈 deploying / upgrading / initializing，一旦
          寫進 `running` 就沒有任何東西會再回頭看。
        - **當初的顧慮不成立**：W5a 認為 VM 合取依賴容器端對空 pods 回 True
          （`cloud_service.py` 舊註解），實測 KubeVirt 的 virt-launcher pod **帶著**
          `app.kubernetes.io/instance` 標籤且回報 `2/2`，所以純 VM 是憑自身條件
          通過的，不靠這個 fail-open。
        - **代價有界**：合法的「release 已 deployed 但 pod 還沒被建出來」窗口只讓
          服務多停留一輪 poll（前台 5 秒 / cron 60 秒），且對已 `running` 的現存
          機隊零影響——它們根本不會被 poll。

        殘餘風險（chart 的 pod 不帶 instance 標籤 → 永久卡 deploying）由合併前
        的 15-template 標籤實證擋住，長期由 #489 的 age timeout 封頂。
        """
        svc = self._make_service('container')

        ready, detail = svc._is_release_ready(_status(pods=[]))

        self.assertFalse(
            ready,
            '沒看過任何一顆 pod 就宣告就緒＝#456 那種「壞掉卻顯示 Running」'
            '（detail=%r）' % detail)

    def test_6_container_with_ready_pods_is_ready(self):
        svc = self._make_service('container')

        ready, _ = svc._is_release_ready(_status(
            pods=[{'name': 'p', 'phase': 'Running', 'ready': '1/1'}],
        ))

        self.assertTrue(ready)

    def test_container_with_unready_pod_is_not_ready(self):
        svc = self._make_service('container')

        ready, _ = svc._is_release_ready(_status(
            pods=[{'name': 'p', 'phase': 'Running', 'ready': '0/1'}],
        ))

        self.assertFalse(ready)

    def test_container_ignores_vm_fields_entirely(self):
        """容器型不看 VM 欄位——即使 operator 意外回了 vmi 錯誤也一樣。"""
        svc = self._make_service('container')

        ready, _ = svc._is_release_ready(_status(
            pods=[{'name': 'p', 'phase': 'Running', 'ready': '1/1'}],
            vm_included=True, vmis=[], vmi_retrieval_error='boom',
        ))

        self.assertTrue(ready)


class TestIncludeVmFlagIsSent(_Base):
    """漏帶 include_vm 會讓 VM 服務永遠卡 deploying —— 用旗標本身寫斷言。"""

    def test_needs_vm_status_true_only_for_vm(self):
        self.assertTrue(self._make_service('vm')._needs_vm_status())
        self.assertFalse(self._make_service('container')._needs_vm_status())

    def test_model_poll_passes_include_vm_for_vm_service(self):
        svc = self._make_service('vm')
        client = MagicMock()
        client.get_status.return_value = _status(
            vm_included=True,
            vmis=[{'name': 'svc-test', 'phase': 'Running', 'ready': True}],
        )

        svc._poll_and_update_status(client)

        self.assertTrue(
            client.get_status.call_args.kwargs['include_vm'],
            "VM 型服務 poll 時必須帶 include_vm=True",
        )

    def test_model_poll_omits_include_vm_for_container_service(self):
        """容器型不帶——省掉 operator 端兩次無謂的 CR 查詢。"""
        svc = self._make_service('container')
        client = MagicMock()
        client.get_status.return_value = _status(
            pods=[{'name': 'p', 'phase': 'Running', 'ready': '1/1'}],
        )

        svc._poll_and_update_status(client)

        self.assertFalse(client.get_status.call_args.kwargs['include_vm'])


class TestModelPollTransitions(_Base):
    """model 層 poll 的端到端狀態轉移。"""

    def test_vm_without_vmis_stays_deploying(self):
        """🔴 端到端版的 #1：整條 poll 跑完，state 必須還是 deploying。"""
        svc = self._make_service('vm')
        client = MagicMock()
        client.get_status.return_value = _status(vm_included=True, vmis=[])

        svc._poll_and_update_status(client)

        self.assertEqual(svc.state, 'deploying')

    def test_vm_with_ready_vmi_becomes_running(self):
        svc = self._make_service('vm')
        client = MagicMock()
        client.get_status.return_value = _status(
            vm_included=True,
            vmis=[{'name': 'svc-test', 'phase': 'Running', 'ready': True}],
            # VM 就緒後仍要過容器閘（`_is_release_ready` 是合取）。實測 KubeVirt
            # 的 virt-launcher pod 帶 `app.kubernetes.io/instance` 且回報 `2/2`，
            # 所以真實世界這裡本來就是非空的——F-423-1 Step 1 之後這個事實變成
            # **必要條件**（舊碼靠容器端對空 pods 回 True 才讓純 VM 通過）。
            pods=[{'name': 'virt-launcher-svc-test', 'phase': 'Running',
                   'ready': '2/2'}],
        )

        svc._poll_and_update_status(client)

        self.assertEqual(svc.state, 'running')

    def test_container_without_pods_stays_deploying(self):
        """🔴 端到端版的 #5：容器型 + 無 pod，整條 poll 跑完必須還是 deploying。

        判定單元測試（#5）與 controller 層（#9）之外再鎖 model poll 這一層——
        #456 的誤報是「寫進 DB 的 state」，所以要有一條直接對 state 斷言的。
        """
        svc = self._make_service('container')
        client = MagicMock()
        client.get_status.return_value = _status(pods=[])

        svc._poll_and_update_status(client)

        self.assertEqual(
            svc.state, 'deploying',
            '沒看過任何一顆 pod 就寫 %r 進 DB' % svc.state)

    def test_import_progress_is_stored_for_display(self):
        """進度落地供卡片顯示；未就緒時正是最有用的時候。"""
        svc = self._make_service('vm')
        client = MagicMock()
        client.get_status.return_value = _status(
            vm_included=True, vmis=[],
            datavolumes=[{'name': 'dv', 'phase': 'ImportInProgress',
                          'progress': '42.31%'}],
        )

        svc._poll_and_update_status(client)

        self.assertEqual(svc.import_progress, '42.31%')
        self.assertEqual(svc.state, 'deploying', "進度不得影響狀態機")

    def test_import_progress_cleared_when_absent(self):
        svc = self._make_service('vm')
        svc.import_progress = '50.0%'
        client = MagicMock()
        client.get_status.return_value = _status(
            vm_included=True,
            vmis=[{'name': 'svc-test', 'phase': 'Running', 'ready': True}],
        )

        svc._poll_and_update_status(client)

        self.assertFalse(svc.import_progress)
