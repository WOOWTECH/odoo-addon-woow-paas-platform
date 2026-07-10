"""#324 stg 清理腳本（冪等、condition-based、預設 DRY_RUN）。

用途：
  1. 把上游 retry cron 產生的 failed deduct 垃圾交易標記為 cancelled
     （保留審計、不刪除；condition：state='failed' 且 transaction_type='deduct'
     且 subscription_id 非空）。
  2. 校正受影響訂閱的 next_payment（condition：in_progress、next_payment < today、
     且「從未成功扣款」＝無 done deduct）→ 推到 today + 一個週期，避免修好後
     仍卡在過去到期日被 dunning 立即重扣（stg 為測試資料）。

執行（leader，在目標 DB 的 odoo shell 內）：
  DRY_RUN 預設 True 只印報告；確認後把 DRY_RUN 改 False 再跑一次。

  kubectl -n <ns> exec -it <odoo-pod> -- \
    odoo shell -c /etc/odoo/odoo.conf -d <db> --no-http \
      < scripts/maintenance/cleanup_324_stg.py
  # 或在互動 shell 內 exec(open('.../cleanup_324_stg.py').read())

冪等性：已 cancelled 的交易、已無 done-deduct-且已在未來的訂閱不會被重複處理。
"""
import logging

from dateutil.relativedelta import relativedelta

from odoo import fields

_logger = logging.getLogger("cleanup_324_stg")

DRY_RUN = True  # ← leader 確認 dry-run 報告後改 False 實跑

_UNIT_DELTA = {
    'day': lambda n: relativedelta(days=n),
    'week': lambda n: relativedelta(weeks=n),
    'month': lambda n: relativedelta(months=n),
    'year': lambda n: relativedelta(years=n),
}


def _has_done_deduct(env, sub):
    return bool(env['wallet.transaction'].sudo().search_count([
        ('subscription_id', '=', sub.id),
        ('transaction_type', '=', 'deduct'),
        ('state', '=', 'done'),
    ]))


def run(env):
    today = fields.Date.today()

    # (1) failed deduct 垃圾 → cancelled（冪等：只抓仍 failed 的）
    garbage = env['wallet.transaction'].sudo().search([
        ('transaction_type', '=', 'deduct'),
        ('state', '=', 'failed'),
        ('subscription_id', '!=', False),
    ])
    print("[#324] failed deduct garbage to cancel: %s" % len(garbage))
    if not DRY_RUN and garbage:
        garbage.write({'state': 'cancelled'})

    # (2) 受影響訂閱 next_payment 校正
    stuck = env['sh.subscription.subscription'].sudo().search([
        ('state', '=', 'in_progress'),
        ('sh_date_of_next_payment', '<', today),
    ])
    affected = [s for s in stuck if not _has_done_deduct(env, s)]
    print("[#324] never-paid overdue subs to re-date: %s" % len(affected))
    for sub in affected:
        delta_fn = _UNIT_DELTA.get(sub.sh_unit)
        new_next = today + delta_fn(sub.sh_recurrency or 1) if delta_fn else today
        print("  sub=%s name=%s next_payment %s -> %s" % (
            sub.id, sub.name, sub.sh_date_of_next_payment, new_next))
        if not DRY_RUN:
            sub.sudo().write({'sh_date_of_next_payment': new_next})

    if not DRY_RUN:
        env.cr.commit()
        print("[#324] committed.")
    else:
        print("[#324] DRY_RUN — no changes written.")


run(env)  # noqa: F821 — odoo shell 注入的 env
