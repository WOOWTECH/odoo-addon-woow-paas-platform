# -*- coding: utf-8 -*-
"""公開 hostname 的 DNS 可解析性探測（#648 (b)）。

平台唯一一處會對「租戶自己的對外網域」做名稱解析的地方。抽成獨立 module 有兩個
理由：(1) 呼叫端一律以 ``dns_probe.resolve_state(...)`` 這個 **module attribute**
形式查名字，所以測試只要 patch 本模組即涵蓋全部呼叫者（見
``docs/decisions/patterns/unittest-mock-dual-namespace-patch.md``——頂部
``from x import y`` 會在每個使用者的 namespace 各複製一份 binding，那才需要 N 個
patch target）；(2) 這裡的失效模式與 Odoo model 無關，獨立測比較誠實。

## 為什麼要自己包 timeout

``socket.getaddrinfo`` **沒有** timeout 參數，逾時完全由 ``/etc/resolv.conf`` 的
``options timeout:N attempts:M`` 決定（未設時 glibc 預設 5 秒 × 2 次）。平台 Odoo
跑在 ``workers = 0``（threaded）＋ ``max_cron_threads = 1``，而
``_cron_check_deploying_services`` 是**逐台序列**跑的 —— resolver 一掛，單一 cron
thread 會被每一台服務各拖住十秒級。所以這裡用一個 module-level executor 把呼叫丟出
去，只等 ``_DEFAULT_TIMEOUT`` 秒，逾時就回 ``'unknown'``（呼叫端據此放行）。

⚠ **刻意不用 ``with ThreadPoolExecutor(...)``**：context manager 離開時會
``shutdown(wait=True)``，正好卡在那條還沒回來的查詢上，timeout 等於白設。掛住的
future 留給它自己結束，pool 大小是它的封頂。

## 為什麼呼叫端要送 FQDN（結尾帶點）

平台 pod 的 ``/etc/resolv.conf`` 是 ``options ndots:5`` ＋ 4 個 search domain，而
``paas-cs-<ws>-<svc>.woowtech.io`` 只有兩個點 ⇒ 相對名會**先跑完 4 個 search
domain 才問絕對名**。實測（prod ``paas-system/paas-odoo``）：查不到的名字，相對名
約 100–216ms、FQDN 約 1–9ms；而 resolver 不可用時最壞值是 5 次查詢 ×（5s × 2）＝
**50 秒** vs FQDN 的 10 秒。本模組會自己補上結尾點當防呆，但呼叫端仍應送 FQDN——
那個性質有測試釘住（``test_probe_is_given_an_fqdn_with_trailing_dot``）。

## 三態的語意（呼叫端的處置刻意不同）

- ``'ok'``      —— 解析得到位址。
- ``'missing'`` —— 權威地回答「沒有位址」：``EAI_NONAME``（NXDOMAIN）或
  ``EAI_NODATA``（名字存在但無 A/AAAA）。**這才是 #648 要擋的那一格。**
- ``'unknown'`` —— resolver 自己不可用（``EAI_AGAIN``）、逾時、或任何其他例外。
  呼叫端**放行**：resolver 不可用是平台自身故障，讓它把每一台租戶服務都扣住，比
  它要偵測的那個 bug 嚴重得多。
"""
import logging
import socket
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout

_logger = logging.getLogger(__name__)

#: 單次探測的上界（秒）。比 glibc 的預設 5s×2 短，因為呼叫端在窗口內每輪都會再問。
_DEFAULT_TIMEOUT = 2.0

#: 逾時的查詢不會被中斷（``getaddrinfo`` 不可取消），只是沒人再等它。pool 大小就是
#: 「同時可以有幾條查詢掛在那裡」的封頂——滿了之後新的呼叫會排隊，因此
#: ``result(timeout=...)`` 仍然是有效的總上界。
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix='woow-dnsprobe')

#: ``socket.gaierror.errno`` 的「權威否定」集合。``EAI_NODATA`` 在部分平台不存在
#: （macOS 有、某些 musl 沒有），所以用 getattr 取。
_NEGATIVE_ERRNOS = {
    getattr(socket, 'EAI_NONAME', -2),   # NXDOMAIN／名字不存在
    getattr(socket, 'EAI_NODATA', -5),   # 名字存在但沒有 A/AAAA
}


def _as_fqdn(hostname):
    """補上結尾點，讓 resolver 跳過 search list。空值原樣回傳給呼叫端自己擋。"""
    if not hostname:
        return hostname
    return hostname if hostname.endswith('.') else hostname + '.'


def _lookup(hostname):
    socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)


def resolve_state(hostname, timeout=_DEFAULT_TIMEOUT):
    """``'ok'`` / ``'missing'`` / ``'unknown'``——語意見 module docstring。

    永不 raise：呼叫端是就緒判定，讓它為了一個探針而爆掉是本末倒置。
    """
    if not hostname:
        return 'unknown'

    fqdn = _as_fqdn(hostname)
    try:
        future = _EXECUTOR.submit(_lookup, fqdn)
    except RuntimeError as exc:          # interpreter 收攤中
        _logger.warning("DNS probe could not be scheduled for %s: %s", fqdn, exc)
        return 'unknown'

    try:
        future.result(timeout=timeout)
        return 'ok'
    except _FutureTimeout:
        # 查詢還掛在 pool 裡；不取消（取消不了），只是不再等它。
        _logger.warning(
            "DNS probe for %s exceeded %.1fs — treating as unknown", fqdn, timeout)
        return 'unknown'
    except socket.gaierror as exc:
        if exc.errno in _NEGATIVE_ERRNOS:
            return 'missing'
        # EAI_AGAIN 之類：resolver 自己不可用，不是這個名字的問題。
        _logger.warning("DNS probe for %s failed (errno=%s): %s",
                        fqdn, exc.errno, exc)
        return 'unknown'
    except Exception as exc:             # noqa: BLE001 —— 探針不得炸掉呼叫端
        _logger.warning("DNS probe for %s raised %s: %s",
                        fqdn, type(exc).__name__, exc)
        return 'unknown'
