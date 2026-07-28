"""假綠守線：防 integration 測試檔漏 marker → 被靜默 deselect（#423 W4 MAJOR-3）。

背景（實際踩過，不是假想）：``pytest.ini`` 的 ``addopts`` 帶 ``-m "not integration"``，
所以 **只有** 自帶 ``pytestmark = pytest.mark.integration`` 的模組才會在
``pytest -m integration`` 被選中。``tests/integration/conftest.py`` 雖然有
module-level ``pytestmark``，但 **conftest 的 pytestmark 不會傳播到同目錄的測試模組**。

漏標的後果特別惡劣：檔案照樣被 collect、卻永遠不執行，兩種指令都是綠的——

- ``pytest``（預設）           → 被 ``-m "not integration"`` 排除？不，**未標的檔會被「選中」**，
  但它裡面的測試需要真叢集，於是在沒有叢集時錯誤地嘗試執行或 error；
- ``pytest -m integration``    → 未標的檔被 **deselect**，報告只顯示 ``N deselected``，
  很容易被讀成「跑完了」。

#423 W4 首次提交就踩到：``test_kubevirt_crd_contract.py`` 漏了 marker，
輸出是 ``31 collected / 5 deselected``、其餘全綠，看起來完全正常。

本檔沿用 ``tests/test_async_harness_guard.py`` 的同型守線做法（AST 靜態掃描，
不依賴本進程當下的 pytest config，避免自我參照假綠）。
"""
import ast
from pathlib import Path

import pytest

OPERATOR_ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_DIR = OPERATOR_ROOT / "tests" / "integration"


def _integration_test_modules():
    return sorted(p for p in INTEGRATION_DIR.glob("test_*.py"))


def _has_integration_pytestmark(path: Path) -> bool:
    """True if the module assigns a module-level pytestmark containing
    ``pytest.mark.integration``.

    Accepts both shapes used in this repo:
        pytestmark = pytest.mark.integration
        pytestmark = [pytest.mark.integration, ...]
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def _mentions_integration(node: ast.AST) -> bool:
        for sub in ast.walk(node):
            # pytest.mark.integration → Attribute(attr='integration')
            if isinstance(sub, ast.Attribute) and sub.attr == "integration":
                return True
        return False

    for node in tree.body:  # module level only — a mark inside a class/def
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):  # would not apply
            continue                                            # to the module
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "pytestmark":
                if _mentions_integration(node.value):
                    return True
    return False


def test_integration_dir_is_not_empty():
    """Guard the guard: an empty glob would make the check below vacuous."""
    modules = _integration_test_modules()
    assert modules, (
        f"no test_*.py found under {INTEGRATION_DIR} — this guard would pass "
        f"vacuously; fix the path"
    )


@pytest.mark.parametrize(
    "module", _integration_test_modules(), ids=lambda p: p.name,
)
def test_every_integration_module_declares_the_marker(module):
    """Every tests/integration/test_*.py must carry the integration marker.

    Without it the module is silently DESELECTED under
    ``pytest -m integration`` — collected, never run, reported green.
    conftest.py's own ``pytestmark`` does NOT propagate here.
    """
    assert _has_integration_pytestmark(module), (
        f"{module.relative_to(OPERATOR_ROOT)} has no module-level "
        f"`pytestmark = pytest.mark.integration`.\n"
        f"Without it, `pytest -m integration` DESELECTS the whole file: it is "
        f"collected but never executed, and the run still reports green.\n"
        f"conftest.py's pytestmark does not propagate to test modules — each "
        f"file must declare its own."
    )
