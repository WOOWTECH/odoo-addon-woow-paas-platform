"""L4 反向孤兒 dry-run 報表 snapshot（TS-5 Phase D）。

cron（Slice B）跑 ``reconcile_orphans`` 後，把分類完的 ``ReconcileResult.orphan_candidates``
落成可呈現的 snapshot（每筆候選一列），供 ``/woow`` admin 後台報表頁（Slice C/D）讀。

**snapshot-replace 語意**：報表是「最新一次對帳的當前快照」，非歷史累積 —— 每次 cron =
清前次 + 寫本次；空 result → 清空（真無孤兒，不保留舊列誤導人工）。

**首版零刪除**：本 model 只「記錄 + 呈現」孤兒清單，刪除權永遠留人手。``_record_snapshot``
的 ``unlink`` 只清自家報表列（Odoo bookkeeping），**非** CF / k8s 資源刪除；delete-gate
（Slice B）對本檔豁免自家 ``unlink``、但仍禁所有 operator client 破壞向量
（``uninstall_release`` / ``scale_release`` / ``delete_*`` / ``subprocess`` …）。
"""
import logging

from odoo import api, fields, models

from ..services.reconciler import reconcile_orphans

_logger = logging.getLogger(__name__)


class ReconcileReport(models.Model):
    _name = 'woow_paas_platform.reconcile_report'
    _description = 'L4 反向孤兒 dry-run 報表 snapshot'
    # reconcilable=True（可回收，首版恆無）排前；其餘按類別 / 名稱穩定排序供人工逐筆審。
    _order = 'reconcilable desc, kind, name'

    resource_uid = fields.Char(
        string='資源 UID',
        required=True,
        index=True,
        help='inventory 資源的唯一識別（CF tunnel / access_app / access_policy id；'
             '未來涵蓋 ns / PVC）。對齊 operator InventoryResponse 的 CfResource.id。',
    )
    name = fields.Char(string='資源名稱')
    kind = fields.Char(
        string='類別',
        help='tunnel / access_app / access_policy …（對齊 CfResource.kind）',
    )
    env_hint = fields.Char(
        string='env 歸屬',
        help="prod / stg / unknown（prod 與 legacy 無 token byte-identical 不可分 → unknown）",
    )
    reconcilable = fields.Boolean(
        string='可回收',
        default=False,
        help='首版恆 False（dry-run 零刪除）；三類 inventory 全 un-reconcilable。'
             'True 僅供未來開 SA route DNS（有 owner marker）後使用。',
    )
    reason = fields.Text(string='判定理由')
    scanned_at = fields.Datetime(
        string='掃描時間',
        help='本筆所屬 snapshot 的對帳時間。人工刪除前須對該筆即時重查（TOCTOU）。',
    )
    partial = fields.Boolean(
        string='不完整掃描',
        default=False,
        help='某類右集 list-all 失敗 → 本次掃描不完整、孤兒可能少報；不可據此清單刪除。',
    )

    @api.model
    def _record_snapshot(self, result):
        """把一次成功的 ``ReconcileResult`` 落成 snapshot（清前次 + 寫本次），回 ``scanned_at``。

        - **snapshot-replace**：先 ``unlink`` 前次全部報表列（自家 bookkeeping，非 CF 資源），
          再 create 本次候選；空 ``orphan_candidates`` → 只清不寫（真無孤兒）。
        - **``.sudo()``**：cron 以任何身份跑都能寫；讀取面的 admin-only 由 ACL（只給
          ``base.group_system``）+ controller ``has_group``（Slice C）兩層守，非靠寫入身份。
        - **no-silent-drop（§C.4 #7）**：忠實落**全部**候選（含 reconcilable=False 的
          cross-env / protected / unknown），不在此層以 reconcilable 過濾。
        """
        Report = self.sudo()
        Report.search([]).unlink()  # 清前次 snapshot（報表列 bookkeeping，非 CF/k8s 資源刪除）
        scanned_at = fields.Datetime.now()
        vals = [{
            'resource_uid': c.id,
            'name': c.name,
            'kind': c.kind,
            'env_hint': c.env_hint,
            'reconcilable': c.reconcilable,
            'reason': c.reason,
            'scanned_at': scanned_at,
            'partial': result.partial,
        } for c in result.orphan_candidates]
        if vals:
            Report.create(vals)
        _logger.info(
            "[RECON_REPORT] snapshot 寫入 %s 筆候選（partial=%s, scanned_at=%s）",
            len(vals), result.partial, scanned_at,
        )
        return scanned_at

    @api.model
    def _cron_reconcile_report(self):
        """低頻反向對帳 cron：跑 ``reconcile_orphans`` → 落 snapshot。**只報告、零刪除**。

        **fail-closed**：``reconcile_orphans`` 任一 abort（R3 env-pin 未鎖 prod|stg /
        operator 未設 / body malformed / 両端 env 不一致 / shared_tunnel_id 缺）→ **保留前次
        snapshot 不洗空**。理由：config 壞掉若洗成空清單，會偽裝成「掃完、無孤兒」——比保留
        （帶舊 scanned_at、人工看得出已 stale）的舊資料更危險。先 ``reconcile_orphans``（可能
        raise）成功後才 ``_record_snapshot``，故 replace 對 abort 原子（不會清了又寫不出）。
        """
        try:
            result = reconcile_orphans(self.env)
        except Exception as exc:
            # dry-run 對帳「本輪沒掃成」是運維狀態非 crash：warning 不洗空、不拋（保留前次
            # snapshot；scanned_at 不前進本身即 stale 訊號）。RuntimeError 訊息已具描述性。
            _logger.warning(
                "[RECON_CRON] L4 反向對帳本輪 abort，保留前次 snapshot（fail-closed）：%s", exc)
            return False
        self._record_snapshot(result)
        return True
