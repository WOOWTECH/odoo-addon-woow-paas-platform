"""TS-1 假綠守線：防 async test 在 operator harness 上被靜默 skip / never-awaited（#208 溫床）。

背景：pytest-asyncio strict mode + ``--disable-warnings`` 下，未標 ``@pytest.mark.asyncio``
的 async test 會被「靜默 SKIPPED + warning 被吞」→ 裡面的 assert 永遠不執行 = 假綠（#208 即此類）。
修法是 ``pytest.ini`` 改 ``asyncio_mode=auto``（未標也執行）+ 移 ``--disable-warnings`` +
``filterwarnings=error``（warning 升 error）。

本檔守線涵蓋 design §Phase A.0 的兩個驗收情境「故意刪某 async test 的 await / marker → CI 須紅」
與「collected async 數 == 預期 且無 test 進 skipped」，全部以**行為證明**（subprocess 跑真
``pytest.ini``，不依賴本進程當下 config，避免自我參照假綠）為主、config 不變式為輔：

- ``test_unmarked_async_test_is_not_silently_skipped`` — 「刪 marker」：未標 async canary 必真執行
  （用唯一 sentinel 斷言 body 真跑，不靠過粗的 '1 failed'，避免 coroutine-error 假裝 failure）。
- ``test_unawaited_coroutine_is_caught`` — 「刪 await」：never-awaited coroutine 必升 error 而紅
  （防有人 ``ignore`` 掉 RuntimeWarning/PytestUnraisableExceptionWarning 把次級防護靜默拆掉）。
- ``test_all_non_integration_async_tests_are_collected`` — collected 涵蓋每個 async test（檔綁定
  比對防同名碰撞；斷言 collect returncode==0 防部分/壞掉的收集被當完整）。
- ``test_no_non_integration_test_is_skipped`` — 跑完非-integration 套件，skipped 數必須 0
  （防 ``@pytest.mark.skip`` 的 reconcile async test 過了 collection 守線卻 run-time 靜默 skip）。

設計依據：``docs/plans/2026-Q2/2026-06-25-l4-reconciler-design.md`` §Phase A.0 / §機械守線。
"""
import ast
import configparser
import re
import subprocess
import sys
from pathlib import Path

OPERATOR_ROOT = Path(__file__).resolve().parent.parent
PYTEST_INI = OPERATOR_ROOT / "pytest.ini"
TESTS_DIR = OPERATOR_ROOT / "tests"
SELF_RELPATH = Path(__file__).resolve().relative_to(OPERATOR_ROOT).as_posix()


def _pytest_ini_section():
    parser = configparser.ConfigParser()
    parser.read(PYTEST_INI, encoding="utf-8")
    return parser["pytest"]


# ── 行為證明（最強守線，非 config 字面斷言）─────────────────────────────

def test_unmarked_async_test_is_not_silently_skipped(tmp_path):
    """「刪 marker」：用 operator 真實 pytest.ini 跑「未標 marker + assert False」的 async canary。

    修前（無 ``asyncio_mode`` + ``--disable-warnings``）：canary 被 SKIPPED → returncode 0
    （假綠）→ 本測試 RED。修後（``asyncio_mode=auto``）：canary 真執行 → assert False → RED→GREEN。

    斷言用唯一 sentinel（canary body 真執行才會把它印進 traceback），**不靠 '1 failed' 字面**——
    後者可被「strict + filterwarnings=error 部分退回」時的 coroutine-never-awaited error 假裝滿足
    （body 其實沒跑）。sentinel 出現 ⟺ body 真執行，airtight。
    """
    sentinel = "TS1_CANARY_BODY_DID_RUN_a7f3c9"
    canary = tmp_path / "test_canary_unmarked_async.py"
    canary.write_text(
        "async def test_canary_must_actually_run():\n"
        f"    assert False, '{sentinel}'\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(canary),
            "-c", str(PYTEST_INI), "--rootdir", str(OPERATOR_ROOT),
            "-p", "no:cacheprovider",
        ],
        capture_output=True, text=True, cwd=str(OPERATOR_ROOT),
    )
    assert result.returncode != 0, (
        "未標 marker 的 async canary 沒讓 pytest 失敗（returncode=0）→ #208 假綠洞仍在。\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert sentinel in result.stdout, (
        "canary body 沒真執行（sentinel 不在輸出）→ 可能是 coroutine-never-awaited error 假裝 "
        "failure 而非 body 真跑。#208 防護未確實生效。\n"
        f"STDOUT:\n{result.stdout}"
    )


def test_unawaited_coroutine_is_caught(tmp_path):
    """「刪 await」：sync test 呼叫 async helper 但漏 await → coroutine-never-awaited 必升 error 而紅。

    這是 design A.0 與「刪 marker」並列的第二個驗收情境，由 ``filterwarnings=error`` 把
    RuntimeWarning('coroutine never awaited') / PytestUnraisableExceptionWarning 升 error 守住。
    若有人未來在 filterwarnings 加 ``ignore::RuntimeWarning`` 等把這次級防護拆掉，本 canary 會變綠
    → 本測試 RED。比僅斷言 ``'error' in filterwarnings`` 的 config 不變式 airtight。
    """
    canary = tmp_path / "test_canary_unawaited.py"
    canary.write_text(
        "async def _helper():\n"
        "    return 1\n"
        "def test_sync_forgets_await():\n"
        "    _helper()  # 漏 await → coroutine never awaited\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(canary),
            "-c", str(PYTEST_INI), "--rootdir", str(OPERATOR_ROOT),
            "-p", "no:cacheprovider",
        ],
        capture_output=True, text=True, cwd=str(OPERATOR_ROOT),
    )
    combined = (result.stdout + result.stderr).lower()
    assert result.returncode != 0, (
        "漏 await 的 coroutine 沒讓 pytest 失敗 → never-awaited 次級防護失效（filterwarnings 被放行？）。\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "never awaited" in combined or "unraisable" in combined, (
        "失敗原因不是 coroutine-never-awaited（可能是別的錯）→ 守線未對準 '刪 await' 情境。\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


# ── config 不變式（belt-and-suspenders；config 漂回舊狀態即紅）──────────

def test_asyncio_mode_is_auto():
    """asyncio_mode=auto → 未標 marker 的 async test 仍被執行，杜絕靜默 skip。"""
    assert _pytest_ini_section().get("asyncio_mode") == "auto"


def test_disable_warnings_not_in_addopts():
    """移除 --disable-warnings → coroutine-never-awaited / async-skip warning 不再被吞。"""
    addopts = _pytest_ini_section().get("addopts", "")
    assert "--disable-warnings" not in addopts


def test_filterwarnings_escalates_to_error():
    """filterwarnings=error → 假綠相關 warning 升為 error（belt-and-suspenders）。

    註：never-awaited 的真正守護是行為型 ``test_unawaited_coroutine_is_caught``，本條只防 config
    整段被清空；故同時斷言沒有把 async 相關 warning 整類 ``ignore`` 掉（降低字面斷言被繞空間）。
    """
    filterwarnings = _pytest_ini_section().get("filterwarnings", "")
    assert "error" in filterwarnings
    assert "ignore::RuntimeWarning" not in filterwarnings
    assert "ignore::pytest.PytestUnraisableExceptionWarning" not in filterwarnings


# ── collection 守線（防 async test 被靜默 drop / 部分收集被當完整）────────

def _async_tests_in_non_integration():
    """用 AST 精準找出非-integration 測試檔的 async 測試函式，回傳 {(relpath, method_name)}。

    刻意用 ast 而非 grep：grep ``async def test_`` 會誤抓字串字面值（本檔 canary 即一例）。
    ``glob('test_*.py')`` 不遞迴 → 天然排除 ``tests/integration/``。
    回傳「檔綁定」tuple（非裸葉名）→ 防跨檔同葉名碰撞遮蔽『某同名 async test 被 drop』。
    """
    found = set()
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        relpath = path.resolve().relative_to(OPERATOR_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("test_"):
                found.add((relpath, node.name))
    return found


def _collected_nodeids():
    """跑 ``pytest --collect-only``，回傳 ({(file_part, leaf)}, returncode, raw_stdout)。

    ``-o addopts=`` 清掉 pytest.ini 的 ``-v``（否則出樹狀 ``<Coroutine>`` 非 ``::`` 節點 ID），
    再顯式補 ``-m "not integration"``。collect-only 不執行測試 → 不遞迴觸發本檔 subprocess guard。
    file_part = nodeid 第一段（tests/xxx.py）；leaf = 末段去 parametrize 後綴。
    """
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "--collect-only", "-q",
            "-o", "addopts=", "-m", "not integration", "-p", "no:cacheprovider",
        ],
        capture_output=True, text=True, cwd=str(OPERATOR_ROOT),
    )
    pairs = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if "::" in line:
            parts = line.split("::")
            pairs.add((parts[0], parts[-1].split("[")[0]))
    return pairs, result.returncode, result.stdout


def test_all_non_integration_async_tests_are_collected():
    """每個非-integration 的 async 測試（檔綁定）都必須真的被 collect（防靜默 drop / 漏跑）。

    搭配 ``test_unmarked_async_test_is_not_silently_skipped`` + ``test_no_non_integration_test_is_skipped``
    （證明 collect 到的會「真執行不被 skip」）→ 合起來守住「async test 數 == 預期 且 無 skip」。
    """
    expected = _async_tests_in_non_integration()
    assert expected, "AST scan 一個 async test 都沒找到 → scan 邏輯壞了（既有應有 24 個）"

    collected, returncode, raw = _collected_nodeids()
    assert returncode == 0, (
        f"collect-only returncode={returncode}（非 0 = collection error）→ 部分/壞掉的收集不可信，"
        "不得在其上判定『無 async test 被 drop』。\n"
        f"collect-only 輸出（尾）：\n{raw[-2000:]}"
    )
    missing = sorted(p for p in expected if p not in collected)
    assert not missing, (
        f"以下 async test（檔::名）未被 collect（恐被靜默 drop / 漏跑）：{missing}\n"
        f"collect-only 輸出：\n{raw}"
    )


def test_no_non_integration_test_is_skipped():
    """design A.0 驗收『無 test 進 skipped』：跑非-integration 全套件，斷言 skipped 數 == 0。

    防『@pytest.mark.skip / skipif 永真 的 async reconcile test 過了 collection subset 守線、
    run-time 卻靜默 SKIPPED、內含 assert 永不執行』這個 #208 同類假綠。``--ignore`` 本檔避免遞迴
    （本檔的 canary 會 spawn subprocess）；本檔自身無任何 skip。本套件現況零刻意 skip，故此為真不變式。
    """
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-m", "not integration", "-rs", "-q",
            "-o", "addopts=", "-p", "no:cacheprovider",
            f"--ignore={SELF_RELPATH}",
        ],
        capture_output=True, text=True, cwd=str(OPERATOR_ROOT),
    )
    # 對稱 collection 守線：先確認 inner 套件真的跑完（0=全綠 / 1=有 fail），否則 collection
    # error / crash（rc 2/3/5）會讓 stdout 無 'N skipped' → skipped 落 0 → 假綠。fail-loud。
    assert result.returncode in (0, 1) and "passed" in result.stdout, (
        f"inner 套件未正常跑完（returncode={result.returncode}）→ skip 判定不可信。\n"
        f"{result.stdout[-2000:]}\nSTDERR:\n{result.stderr[-1000:]}"
    )
    match = re.search(r"(\d+) skipped", result.stdout)
    skipped = int(match.group(1)) if match else 0
    assert skipped == 0, (
        f"非-integration 套件出現 {skipped} 個 skipped（恐有 reconcile test 被 @pytest.mark.skip "
        "靜默關掉、assert 永不執行）。逐筆原因見下方 -rs 區。\n"
        f"{result.stdout[-2000:]}"
    )
