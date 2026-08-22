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

## resolver 整個掛掉時的退避

``'unknown'`` 的處置是「放行、而且連旗標都不動」⇒ resolver 不可用時，每一次探測都是
「付滿 timeout、拿零資訊」。所以本模組對**resolver 級**的失效（逾時 / ``EAI_AGAIN``）
上一個 ``_RESOLVER_BACKOFF_SECONDS`` 的閂，窗口內直接回 ``'unknown'`` 不再送查詢。
**不對逐主機名的例外上閂**——理由見該常數的註解。

## 三態的語意（呼叫端的處置刻意不同）

- ``'ok'``      —— 解析得到位址。
- ``'missing'`` —— 權威地回答「沒有位址」：``EAI_NONAME``（NXDOMAIN）或
  ``EAI_NODATA``（名字存在但無 A/AAAA）。**這才是 #648 要擋的那一格。**
- ``'unknown'`` —— resolver 自己不可用（``EAI_AGAIN``）、逾時、或任何其他例外。
  呼叫端**放行**：resolver 不可用是平台自身故障，讓它把每一台租戶服務都扣住，比
  它要偵測的那個 bug 嚴重得多。
"""
import logging
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout

_logger = logging.getLogger(__name__)

#: 單次探測的上界（秒）。比 glibc 的預設 5s×2 短，因為呼叫端在窗口內每輪都會再問。
_DEFAULT_TIMEOUT = 2.0

#: resolver 整個不可用時的退避窗口（秒）。窗口內直接回 ``'unknown'``，**不再送出
#: 任何查詢**。
#:
#: 為什麼需要：``'unknown'`` 這一態的處置是「放行、而且**連旗標都不動**」——也就是
#: 說，resolver 掛掉時每一次探測都是「付滿 ``_DEFAULT_TIMEOUT``、拿回零資訊」。而
#: 平台 Odoo 是 ``workers = 0`` ＋ ``max_cron_threads = 1``、
#: ``_cron_check_deploying_services`` 逐台序列跑，``limit_time_real = 120``
#: ⇒ 轉移中的服務一多，那條 cron 會被純粹的等待吃光。上閂之後，每個行程每
#: ``_RESOLVER_BACKOFF_SECONDS`` 最多付一次滿額 timeout。
#:
#: 🔴 **只對 resolver 級的失效上閂**（逾時、``EAI_AGAIN``），**不對逐主機名的例外
#: 上閂**。理由：``except Exception`` 那條也回 ``'unknown'``，但它可能是這個名字自己
#: 的問題（實測過一個：全是點的主機名讓 IDNA 編碼拋 ``UnicodeEncodeError``）。拿一
#: 個壞名字去斷定「resolver 掛了」，會讓其他健康的主機名跟著 30 秒不被探測——那是
#: 用一個壞輸入癱瘓整個守線。
_RESOLVER_BACKOFF_SECONDS = 30.0

#: ``time.monotonic()`` 的時間點；小於它就當 resolver 仍不可用。
#: 純量、無鎖：競態最壞是多寫一次同性質的值，代價遠小於一把可能跨 fork 的鎖。
_resolver_down_until = 0.0


def _latch_resolver_down(reason):
    global _resolver_down_until
    _resolver_down_until = time.monotonic() + _RESOLVER_BACKOFF_SECONDS
    _logger.warning(
        "DNS resolver looks unavailable (%s) — skipping probes for %.0fs",
        reason, _RESOLVER_BACKOFF_SECONDS)


def _resolver_is_latched_down():
    return time.monotonic() < _resolver_down_until

#: 逾時的查詢不會被中斷（``getaddrinfo`` 不可取消），只是沒人再等它。pool 大小就是
#: 「同時可以有幾條查詢掛在那裡」的封頂——滿了之後新的呼叫會排隊，因此
#: ``result(timeout=...)`` 仍然是有效的總上界。
_MAX_WORKERS = 4

_EXECUTOR = None
_EXECUTOR_PID = None


def _reset_after_fork():
    """fork 之後子行程手上那個 executor 是廢的——丟掉，讓下次呼叫重建。

    **不要對舊的呼叫 ``shutdown()``**：它的 worker thread 在子行程裡根本不存在，
    ``shutdown(wait=True)`` 會當場卡死；``wait=False`` 也只是把一個永遠不會被消化
    的 queue 留著。直接放生，讓 GC 處理。
    """
    global _EXECUTOR, _EXECUTOR_PID, _resolver_down_until
    _EXECUTOR = None
    _EXECUTOR_PID = None
    # 閂也一起重置：子行程繼承來的 monotonic 時間點沒有意義。
    _resolver_down_until = 0.0


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _executor():
    """取得**本行程自己的** executor。

    🔴 為什麼不能直接用一個 module-level 的常數（實測，不是推測）：
    ``ThreadPoolExecutor`` 在 module import 時就建好，若行程之後 ``fork()``
    （Odoo 的 ``workers > 0`` prefork 模式），子行程繼承到的是一個**廢掉但看起來
    正常**的 executor：

    - ``_threads`` 集合裡有 2 個 thread 物件，但 ``is_alive()`` 全是 ``False``
      （thread 不跨 fork）。
    - 更關鍵的是 ``_idle_semaphore`` **也被繼承**，計數來自 fork 前那些已經
      idle 的 worker ⇒ 子行程呼叫 ``submit()`` 時，``_adjust_thread_count()``
      的第一件事 ``self._idle_semaphore.acquire(timeout=0)`` 會回 ``True``、
      直接 ``return``，**一條新 worker 都不會開**。
    - 於是工作被放進 ``_work_queue``（qsize 1）卻沒有任何人消化，future 永遠
      不完成。

    （實測輸出：``child _threads=2 alive=0``、``idle_semaphore.acquire(timeout=0)
    -> True``、``qsize after submit = 1``、``future -> HANG``。）

    🔴 **為什麼這件事值得多寫這幾行**：壞掉的表現是 ``result(timeout=...)`` 逾時
    ⇒ 本模組回 ``'unknown'`` ⇒ 呼叫端**放行** ⇒ DNS 閘永遠不擋。而「閘永遠不擋」
    與「DNS 一直都很正常」在所有可觀測訊號上完全一樣：沒有 error、測試全綠、服務
    照樣翻 running。那正好讓這個 feature 存在的唯一理由靜靜地失效。今天 prod 是
    ``workers = 0``（不 fork）所以碰不到——但那是**設定值不是不變式**，改設定的人
    不會知道要來看這裡。

    以 pid 為準重建，而**刻意不加 module-level lock**：fork 時若剛好有別的 thread
    持著那把鎖，子行程會繼承一把永遠不會被釋放的鎖——那正是我們要移除的那類靜默
    卡死。這裡的競態最壞是同時建出兩個 executor、其中一個被丟掉（幾條 idle
    thread），代價遠小於一把死鎖。``register_at_fork`` 是主要機制，pid 檢查是它
    不存在時（或 spawn／forkserver 之外的邊角）的後備。
    """
    global _EXECUTOR, _EXECUTOR_PID
    pid = os.getpid()
    if _EXECUTOR is None or _EXECUTOR_PID != pid:
        _EXECUTOR = ThreadPoolExecutor(
            max_workers=_MAX_WORKERS, thread_name_prefix='woow-dnsprobe')
        _EXECUTOR_PID = pid
    return _EXECUTOR

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

    if _resolver_is_latched_down():
        # 退避窗口內：不送查詢，直接回報未知（呼叫端的處置與真的探到 unknown 相同）。
        return 'unknown'

    fqdn = _as_fqdn(hostname)
    try:
        future = _executor().submit(_lookup, fqdn)
    except RuntimeError as exc:          # interpreter 收攤中
        _logger.warning("DNS probe could not be scheduled for %s: %s", fqdn, exc)
        return 'unknown'

    try:
        future.result(timeout=timeout)
        return 'ok'
    except _FutureTimeout:
        # 查詢還掛在 pool 裡；不取消（取消不了），只是不再等它。
        _latch_resolver_down('probe for %s exceeded %.1fs' % (fqdn, timeout))
        return 'unknown'
    except socket.gaierror as exc:
        if exc.errno in _NEGATIVE_ERRNOS:
            return 'missing'
        # EAI_AGAIN 之類：resolver 自己不可用，不是這個名字的問題 ⇒ 上閂。
        _latch_resolver_down('errno=%s on %s (%s)' % (exc.errno, fqdn, exc))
        return 'unknown'
    except Exception as exc:             # noqa: BLE001 —— 探針不得炸掉呼叫端
        # 刻意**不**上閂：這一類可能是這個名字自己的問題（例如 IDNA 編碼失敗），
        # 拿它斷定 resolver 掛了會讓其他健康的主機名一起被停掉探測。
        _logger.warning("DNS probe for %s raised %s: %s",
                        fqdn, type(exc).__name__, exc)
        return 'unknown'
