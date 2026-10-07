"""#1552：同時執行的 helm 寫入類作業（install／upgrade／rollback／uninstall，含 dry-run）上限。

為什麼要限：helm／kubectl 子行程都跑在 operator 同一個容器、同一個 memory cgroup。2026-10-07
正式站三台服務在約 10 秒內各送一次「修改配置」，operator 同時起了三個 helm（每個約 200MiB）
⇒ 512Mi 的容器被 OOMKilled（整個容器一起殺，`memory.oom.group=1`），進行中的請求全斷。

設計：

* 一個 process 內共用的 ``threading.BoundedSemaphore``，大小 ``HELM_MAX_CONCURRENT_OPERATIONS``。
  FastAPI 的同步端點各跑在 threadpool 的一條 thread 上，同一顆 semaphore 就管得到。
* **同一條 thread 可重入**：端點可以把「預檢 dry-run＋真正 upgrade」整段包成一個 slot
  （``upgrade_release``），裡面 ``HelmService.upgrade`` 再進來時不重複排隊——否則一個已經
  通過預檢的請求可能在第二段又排一次隊、甚至排不到。
* 排隊有上限 ``HELM_QUEUE_TIMEOUT_SECONDS``；等不到就 raise :class:`HelmOperationBusy`
  （``src.main`` 轉成 503 ＋ ``error_code=operator_busy``）。拿不到 slot 時什麼都還沒做，
  呼叫端可以安全地重送。
* 只限寫入類：查詢類（list／status／history／get）每次約 40MiB、零點幾秒，平台的狀態輪詢
  不應該排在一個要跑好幾分鐘的 upgrade 後面。
"""
import contextlib
import logging
import threading
import time
from typing import Iterator, Optional

from src.config import settings

logger = logging.getLogger(__name__)

#: 回給平台的 503 ``detail.error_code``（平台 ``PaaSOperatorClient`` 依它顯示中文說明）。
OPERATOR_BUSY_ERROR_CODE = "operator_busy"


class HelmOperationBusy(Exception):
    """排隊超過 ``helm_queue_timeout_seconds`` 仍拿不到 helm slot（什麼都沒套用）。"""

    def __init__(self, operation: str, waited_seconds: float, limit: int):
        self.operation = operation
        self.waited_seconds = waited_seconds
        self.limit = limit
        super().__init__(
            f"helm {operation}: no free slot after {waited_seconds:.0f}s "
            f"(HELM_MAX_CONCURRENT_OPERATIONS={limit})"
        )

    def detail(self) -> dict:
        """503 的結構化 detail（中文訊息給管理員／平台 log；平台另有自己的租戶文案）。"""
        return {
            "error_code": OPERATOR_BUSY_ERROR_CODE,
            "message": (
                "部署服務目前同時處理的 Helm 作業已達上限（%d 個），排隊 %d 秒仍未輪到；"
                "這次的變更沒有套用，請稍後再送一次。" % (self.limit, int(self.waited_seconds))
            ),
            "retryable": True,
        }


class _HelmOperationLimiter:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._semaphore: Optional[threading.BoundedSemaphore] = None
        self._size = 0
        self._local = threading.local()
        self._active = 0
        self._waiting = 0

    def _get_semaphore(self) -> threading.BoundedSemaphore:
        with self._guard:
            if self._semaphore is None:
                self._size = max(1, int(settings.helm_max_concurrent_operations))
                self._semaphore = threading.BoundedSemaphore(self._size)
            return self._semaphore

    def reset(self) -> None:
        """測試用：下次使用時依目前的 settings 重建 semaphore。"""
        with self._guard:
            self._semaphore = None
            self._size = 0
            self._active = 0
            self._waiting = 0

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return self._waiting

    @contextlib.contextmanager
    def slot(self, operation: str, timeout: Optional[float] = None) -> Iterator[None]:
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return

        semaphore = self._get_semaphore()
        if timeout is None:
            timeout = settings.helm_queue_timeout_seconds
        started = time.monotonic()
        with self._guard:
            self._waiting += 1
        try:
            acquired = semaphore.acquire(timeout=timeout)
        finally:
            with self._guard:
                self._waiting -= 1
        waited = time.monotonic() - started
        if not acquired:
            logger.warning(
                "helm %s refused: no free slot after %.1fs (limit %d, active %d)",
                operation, waited, self._size, self._active,
            )
            raise HelmOperationBusy(operation, waited, self._size)
        if waited >= 1:
            logger.info("helm %s waited %.1fs for a slot (limit %d)", operation, waited, self._size)
        with self._guard:
            self._active += 1
        self._local.depth = 1
        try:
            yield
        finally:
            self._local.depth = 0
            with self._guard:
                self._active -= 1
            semaphore.release()


_limiter = _HelmOperationLimiter()


def helm_operation_slot(operation: str, timeout: Optional[float] = None):
    """``with helm_operation_slot("upgrade"):`` ⇒ 佔一個 helm 寫入類作業的名額（可重入）。

    ``timeout``：這次最多排隊幾秒（預設 ``HELM_QUEUE_TIMEOUT_SECONDS``）。有自己預算階梯的
    路徑（reset-config，#461）傳更短的值，並把它算進該階梯的硬上限。
    """
    return _limiter.slot(operation, timeout)


def reset_helm_operation_limiter() -> None:
    """測試用：改了 ``settings.helm_max_concurrent_operations`` 之後重建。"""
    _limiter.reset()


def helm_operation_stats() -> dict:
    return {"active": _limiter.active, "waiting": _limiter.waiting}
