"""Discovery 的 bounded metadata GET；每批結束才由 caller 做授權判斷。"""
from concurrent.futures import ThreadPoolExecutor
import time

from .transport import MessageError

MAX_PARALLEL_READS = 4


def read_batch(items, fetch, deadline):
    """最多四個並行 GET，依輸入順序回傳 (item, value, error)。

    只用於 metadata 讀取；錯誤仍由 caller 的既有 fail-closed／subtree 分流處理。
    批次全部 join 後才回傳，不留背景讀取，也不與 caller 的下一個 HTTP 操作疊加。
    """
    if len(items) > MAX_PARALLEL_READS:
        raise ValueError('discovery_read_batch_too_large')
    if time.monotonic() >= deadline:
        raise MessageError('registration_scan_incomplete')
    results = []
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_READS) as executor:
        futures = [executor.submit(fetch, item, deadline) for item in items]
        for item, future in zip(items, futures):
            try:
                results.append((item, future.result(), None))
            except MessageError as exc:
                results.append((item, None, exc))
    if time.monotonic() >= deadline:
        raise MessageError('registration_scan_incomplete')
    return results
