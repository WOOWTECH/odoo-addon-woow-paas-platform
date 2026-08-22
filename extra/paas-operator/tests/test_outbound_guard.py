"""#841 守線自身的測試：真實 outbound 攔截器與它那一格例外。

本檔全部在**預設** ``pytest``（不加任何 ``-m``）就會被執行 —— 這是刻意的。
本 repo 沒有任何 CI 會跑 operator 的 pytest 套件，只在 ``-m integration`` 下才跑的
守線等於沒有守線，而那正是 #841 要治的病。

零出網：所有「企圖出網」的目標都是 ``127.0.0.1:1``（保證無人監聽），
被擋時根本不會建立 socket，沒被擋時得到的是 ECONNREFUSED。
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pytest

from tests.conftest import (
    OUTBOUND_BLOCKED_PREFIX,
    OUTBOUND_EXEMPT_MARKER,
    RealOutboundAttempted,
)

OPERATOR_ROOT = Path(__file__).resolve().parent.parent

#: 保證無人監聽的目標。走到這裡代表守線沒攔住（會拿到 ECONNREFUSED）；
#: 被攔住的話連 socket 都不會開。
DEAD_TARGET = "http://127.0.0.1:1/probe-841"


# ==================== 陽性對照：攔截器真的有牙 ====================

def test_sync_outbound_is_blocked():
    """同步路徑（src/services/webhook_dispatcher.py 的 httpx.post 走這條）。"""
    with pytest.raises(RealOutboundAttempted, match=OUTBOUND_BLOCKED_PREFIX):
        httpx.get(DEAD_TARGET, timeout=2.0)


@pytest.mark.asyncio
async def test_async_outbound_is_blocked():
    """非同步路徑（src/services/cloudflare.py 全部的 httpx.AsyncClient 走這條）。"""
    with pytest.raises(RealOutboundAttempted, match=OUTBOUND_BLOCKED_PREFIX):
        async with httpx.AsyncClient() as client:
            await client.get(DEAD_TARGET, timeout=2.0)


def test_blocked_message_names_the_method_and_the_url():
    """紅在哪比紅了重要：訊息要指名是哪個動詞打去哪裡，否則追不到是哪支測試漏 patch。"""
    with pytest.raises(RealOutboundAttempted) as excinfo:
        httpx.delete(DEAD_TARGET, timeout=2.0)

    message = str(excinfo.value)
    assert OUTBOUND_BLOCKED_PREFIX in message
    assert "DELETE" in message
    assert "127.0.0.1:1" in message
    # 訊息要能自帶修法，不然撞到的人只知道被擋、不知道下一步。
    assert f"pytest.mark.{OUTBOUND_EXEMPT_MARKER}" in message


# ==================== 陰性對照：不該命中的輸入不會被誤傷 ====================

@pytest.mark.asyncio
async def test_in_process_asgi_is_not_blocked():
    """ASGITransport 不碰 socket ⇒ 不該被攔（TestClient 測試靠這個性質活著）。

    這是判準的陰性對照：換成一個「不該命中」的輸入，攔截器必須不作聲。
    """
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok-841"})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://asgi.invalid") as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.text == "ok-841"


def test_fastapi_testclient_is_not_blocked():
    """starlette 的 _TestClientTransport 直接繼承 BaseTransport，不是 HTTPTransport 子類。

    套件裡 tests/test_api.py 等整批 TestClient 測試的存活條件就是這一條，
    所以把它釘成斷言而不是留在註解裡。
    """
    from starlette.testclient import _TestClientTransport

    assert not issubclass(_TestClientTransport, httpx.HTTPTransport)
    assert not issubclass(_TestClientTransport, httpx.AsyncHTTPTransport)


def test_guard_scope_is_httpx_only():
    """守線範圍＝httpx 這一層。subprocess（helm/kubectl）**刻意**不在內（#841 決策 2）。

    釘成斷言的理由：日後有人順手擴大時，這一格會紅，逼他回來改決策紀錄，
    而不是讓「只擋 httpx」這個宣稱靜靜地變成假的。
    """
    assert httpx.AsyncHTTPTransport.handle_async_request.__name__ == "_boom_async"
    assert httpx.HTTPTransport.handle_request.__name__ == "_boom_sync"

    assert subprocess.run.__module__ == "subprocess"
    assert subprocess.Popen.__module__ == "subprocess"


def test_exempt_marker_is_the_one_the_default_run_deselects():
    """例外集合必須恰好等於預設 run 排除掉的那一組。

    這是「用 marker 而不是路徑判斷」的整個立論所在：拿得到例外的測試，
    同時就是預設 ``pytest`` 不會執行的測試。有人把 addopts 的 ``-m`` 改掉、
    或改了例外 marker 名字，這個等式就破了 —— 那一刻要有人喊。
    """
    ini = (OPERATOR_ROOT / "pytest.ini").read_text(encoding="utf-8")

    assert f'-m "not {OUTBOUND_EXEMPT_MARKER}"' in ini, (
        f"pytest.ini 的 addopts 不再是 -m \"not {OUTBOUND_EXEMPT_MARKER}\"；"
        f"例外集合已與預設 run 排除的集合脫鉤"
    )
    assert "--strict-markers" in ini, (
        "pytest.ini 少了 --strict-markers；marker 打錯字會靜默失效，"
        "例外那格就沒有機器守線在撐了"
    )
    assert f"\n    {OUTBOUND_EXEMPT_MARKER}:" in ini, (
        f"pytest.ini 的 markers 區段沒有註冊 {OUTBOUND_EXEMPT_MARKER}"
    )


# ==================== 例外那一格：整段 pytest 實跑 ====================

_SYNTH_PROBE = textwrap.dedent(
    '''
    """合成探針：記錄「企圖出網」實際得到什麼例外，不做判斷。

    判斷留在外層真套件的 assert 裡 —— 探針只負責產出可觀測值。
    """
    import os
    import httpx
    {marker_line}

    TARGET = "http://127.0.0.1:1/probe-841"


    def _record(label, outcome):
        with open(os.environ["PROBE_OUT"], "a", encoding="utf-8") as fh:
            fh.write(f"{{label}}={{outcome}}\\n")


    def test_sync_attempt():
        try:
            httpx.get(TARGET, timeout=2.0)
        except BaseException as exc:
            _record("{label}.sync", type(exc).__name__)
        else:
            _record("{label}.sync", "NO_EXCEPTION")


    async def test_async_attempt():
        try:
            async with httpx.AsyncClient() as client:
                await client.get(TARGET, timeout=2.0)
        except BaseException as exc:
            _record("{label}.async", type(exc).__name__)
        else:
            _record("{label}.async", "NO_EXCEPTION")
    '''
)

_SYNTH_INI = textwrap.dedent(
    """
    [pytest]
    testpaths = .
    python_files = test_*.py
    addopts = -q --strict-markers -m "not integration"
    asyncio_mode = auto
    markers =
        integration: Integration tests requiring a live K8s cluster
    """
).lstrip()

_SYNTH_CONFTEST = textwrap.dedent(
    """
    import os
    import sys

    sys.path.insert(0, os.environ["OPERATOR_ROOT"])

    if os.environ["GUARD"] == "on":
        # 匯入的是**正式那一支** fixture 物件本身，不是複製品。
        from tests.conftest import block_real_outbound  # noqa: F401
    """
).lstrip()


def _build_synthetic_suite(root: Path) -> None:
    (root / "pytest.ini").write_text(_SYNTH_INI, encoding="utf-8")
    (root / "conftest.py").write_text(_SYNTH_CONFTEST, encoding="utf-8")
    (root / "test_plain.py").write_text(
        _SYNTH_PROBE.format(marker_line="", label="plain"), encoding="utf-8"
    )
    (root / "test_marked.py").write_text(
        _SYNTH_PROBE.format(
            marker_line="import pytest\n\npytestmark = pytest.mark.integration",
            label="marked",
        ),
        encoding="utf-8",
    )


def _run_synthetic(root: Path, out: Path, guard: str, marker_args: list) -> dict:
    if out.exists():
        out.unlink()
    env = dict(os.environ)
    env["OPERATOR_ROOT"] = str(OPERATOR_ROOT)
    env["PROBE_OUT"] = str(out)
    env["GUARD"] = guard
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *marker_args],
        cwd=root, env=env, capture_output=True, text=True, timeout=180,
    )
    assert completed.returncode == 0, (
        f"合成套件本身沒跑成功（guard={guard} args={marker_args}）：\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    assert out.exists(), (
        f"合成探針沒有寫出任何觀測值（guard={guard} args={marker_args}），"
        f"這代表它根本沒被執行，不是「沒有例外」：\n{completed.stdout}"
    )
    return dict(
        line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines()
    )


def test_exempt_marker_lets_integration_reach_the_real_transport(tmp_path):
    """三跑對照：守線開/關 × 有/無 marker，判準必須跟著翻面。

    ``httpx.ConnectError``（ECONNREFUSED）只可能來自**真的開了 socket**，
    所以它不只代表「沒被擋」，還正面證明了那條請求走完了真 transport ——
    這比「沒看到攔截例外」有力，後者也可能是探針根本沒跑。
    """
    _build_synthetic_suite(tmp_path)
    out = tmp_path / "probe.out"

    # 跑 1｜守線開 + 預設 run：未標記的測試 → 必須被攔。
    guarded_plain = _run_synthetic(tmp_path, out, guard="on", marker_args=[])
    assert guarded_plain == {
        "plain.sync": "RealOutboundAttempted",
        "plain.async": "RealOutboundAttempted",
    }, guarded_plain

    # 跑 2｜守線開 + -m integration：標記過的測試 → 必須放行到真 socket。
    guarded_marked = _run_synthetic(
        tmp_path, out, guard="on", marker_args=["-m", "integration"]
    )
    assert guarded_marked == {
        "marked.sync": "ConnectError",
        "marked.async": "ConnectError",
    }, guarded_marked

    # 跑 3｜陰性對照，守線關 + 預設 run：同一支未標記的測試 → 判準翻面。
    # 少了這一跑，跑 1 的結果也可能是探針自己壞掉造成的。
    unguarded_plain = _run_synthetic(tmp_path, out, guard="off", marker_args=[])
    assert unguarded_plain == {
        "plain.sync": "ConnectError",
        "plain.async": "ConnectError",
    }, unguarded_plain
