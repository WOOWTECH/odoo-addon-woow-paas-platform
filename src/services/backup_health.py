# -*- coding: utf-8 -*-
"""備份狀態的平台端判定（#1190／#1265；商用驗收 X-BK-006-1、DATA-ODOO-005-2、AI-LLM-009-1）。

純函式、不 import odoo（同 ``woow_odoo_readiness.py``／``workload_health.py``）：輸入是
operator ``GET /api/releases/{ns}/{name}/backup-status`` 的回應與服務列上已存的事實，
輸出是要寫回 ``cloud_service`` 的新事實。寫不寫、何時寫由 model 決定
（``CloudService._apply_backup_observation``）。

## 事實（服務列上的五格）

- ``last_backup_success_at``：最近一次成功備份的開始時間（UTC）。**只前進不後退**——
  operator 讀不到（Job 被回收、DB pod 沒在跑、舊 chart）或回報較舊的時間時保留舊值（#1265 Q41）。
- ``last_backup_failure_at``／``backup_failure_reason``：最近一次失敗的時間與原因代碼；
  同樣只前進（成功之後不清，判定時比時間先後）。
- ``backup_consecutive_failures``：operator 回報的連續失敗次數（讀不到時保留）。
- ``backup_health``：下面的判定結果。

## 判定（``ok``／``failed``／``stale``／``unknown``）

1. operator 說這個 release 沒有備份機制（``supported: false``）或備份被關掉 ⇒ ``unknown``。
2. 最近一次失敗晚於最近一次成功 ⇒ ``failed``。
3. 錨點＝``max(last_backup_success_at, deployed_at)``；``now − 錨點 > 27h`` ⇒ ``stale``。
   27h＝24h 排程間隔＋``startingDeadlineSeconds`` 600s＋``activeDeadlineSeconds`` 3600s＋
   餘裕（woow-odoo chart ``templates/backup-cronjob.yaml``；Asia/Taipei 無 DST）。
   litellm 的 daily dump 也是每 24h 一次（``backup.hourUtc``），用同一個門檻：pod 在那個
   小時剛好沒在跑時會延到 48h 補做，那段期間顯示 stale 是事實（確實少了一份）。
4. 有成功紀錄 ⇒ ``ok``；從沒成功過、也還沒超過 27h ⇒ ``unknown``。

只是事實與提示，**不影響服務 state**（#1251 Q6：例行 backup Job 不參與 readiness）。
"""
from datetime import datetime, timedelta, timezone

#: 有備份機制、由 operator backup-status 回報的 chart（依 template ``helm_chart_name`` 最後一段）。
BACKUP_CHARTS = frozenset({'woow-odoo', 'litellm'})

#: 多久沒有成功備份算過期（推導見模組 docstring）。刻意是常數、不開 config_parameter。
BACKUP_STALE_HOURS = 27

HEALTH_OK = 'ok'
HEALTH_FAILED = 'failed'
HEALTH_STALE = 'stale'
HEALTH_UNKNOWN = 'unknown'

#: 原因代碼：只收 operator／chart 定義過的字元集合，其他一律 ``unknown``（不轉送任意字串）。
_REASON_CHARS = set('abcdefghijklmnopqrstuvwxyz0123456789_-')
_REASON_MAX = 64


def _parse(value):
    """operator 的 ``…Z`` ISO 字串 → naive UTC datetime（Odoo Datetime 欄位的形狀）。"""
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def reason_code(value):
    if not isinstance(value, str) or not value:
        return False
    value = value.strip().lower()
    if len(value) > _REASON_MAX or not set(value) <= _REASON_CHARS:
        return 'unknown'
    return value


def _later(current, candidate):
    if candidate and (not current or candidate > current):
        return candidate
    return current


def compute_health(supported, enabled, success_at, failure_at, deployed_at, now):
    """判定（規則見模組 docstring）。datetime 一律 naive UTC。"""
    if not supported or enabled is False:
        return HEALTH_UNKNOWN
    if failure_at and (not success_at or failure_at > success_at):
        return HEALTH_FAILED
    anchor = max([d for d in (success_at, deployed_at) if d], default=None)
    if anchor and now - anchor > timedelta(hours=BACKUP_STALE_HOURS):
        return HEALTH_STALE
    if success_at:
        return HEALTH_OK
    return HEALTH_UNKNOWN


def next_backup_facts(current, payload, deployed_at, now):
    """由 operator 回應與既有事實算出新的五格。

    Args:
        current: dict，鍵＝``last_backup_success_at``／``last_backup_failure_at``／
            ``backup_failure_reason``／``backup_consecutive_failures``（值為 Odoo 讀出的
            naive UTC datetime／str／int／False）。
        payload: operator 回應 dict；``None`` ＝ 這一輪讀取失敗（只重算 health，事實不動）。
        deployed_at: 服務的 ``deployed_at``（naive UTC）或 None。
        now: naive UTC datetime。

    Returns:
        dict：同上四鍵＋``backup_health``。
    """
    success_at = current.get('last_backup_success_at') or None
    failure_at = current.get('last_backup_failure_at') or None
    reason = current.get('backup_failure_reason') or False
    consecutive = current.get('backup_consecutive_failures') or 0
    supported, enabled = True, True
    if isinstance(payload, dict):
        supported = payload.get('supported') is True
        enabled = payload.get('enabled') is not False
        if supported and payload.get('status_recorded') is not False:
            success_at = _later(success_at, _parse(payload.get('last_success_at')))
            reported_failure = _parse(payload.get('last_failure_at'))
            if reported_failure and (not failure_at or reported_failure >= failure_at):
                failure_at = reported_failure
                reason = reason_code(payload.get('last_failure_reason')) or 'unknown'
            reported_consecutive = payload.get('consecutive_failures')
            if isinstance(reported_consecutive, int) and not isinstance(reported_consecutive, bool) \
                    and 0 <= reported_consecutive <= 1000:
                consecutive = reported_consecutive
    return {
        'last_backup_success_at': success_at or False,
        'last_backup_failure_at': failure_at or False,
        'backup_failure_reason': reason or False,
        'backup_consecutive_failures': consecutive,
        'backup_health': compute_health(supported, enabled, success_at, failure_at, deployed_at, now),
    }
