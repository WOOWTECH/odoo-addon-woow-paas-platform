"""build.py 的同步檢查（不需 omnigent，CI 可跑）。

`build.py --check` 的第 0 項在本機才跑得到（其餘項目要 import omnigent）。CI 沒有 omnigent，
#1922 改 generate-worker.py 後 config.yaml 沒重產就進了 develop（#1928）。這裡把第 0 項搬進
unittest；security-sweep-tests.yml 在 generate-worker.py 變動時也會觸發。

執行：python3 -m unittest discover -s .omnigent/woow-security-sweep/tests -v
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SWEEP = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("sweep_build", SWEEP / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


class TestConfigInSync(unittest.TestCase):
    def test_config_yaml_matches_build_output(self):
        self.assertEqual(
            build.CONFIG.read_text(encoding="utf-8"), build.config_text(),
            "config.yaml 與 build.py 不同步：重跑 python3 .omnigent/woow-security-sweep/build.py",
        )

    def test_file_tools_has_no_duplicates(self):
        self.assertEqual(len(build.FILE_TOOLS), len(set(build.FILE_TOOLS)))


class TestHostAgnostic(unittest.TestCase):
    """agent 照著跑的文件不得綁主機（#1934：排程改在 pi3，workspace 不是 /home/elmo）。"""

    RUNTIME_DOCS = ["AGENTS.md", *sorted(str(p.relative_to(SWEEP)) for p in SWEEP.glob("skills/*/SKILL.md"))]

    def test_runtime_docs_use_ws_placeholder(self):
        for rel in self.RUNTIME_DOCS:
            text = (SWEEP / rel).read_text(encoding="utf-8")
            with self.subTest(doc=rel):
                self.assertNotIn("/home/elmo", text)

    def test_runtime_docs_pin_python312(self):
        # collect.py 需要 tarfile.data_filter；裸 python3 在 pi3 是 3.11.2
        for rel in self.RUNTIME_DOCS:
            text = (SWEEP / rel).read_text(encoding="utf-8")
            with self.subTest(doc=rel):
                self.assertNotRegex(text, r"python3 [<~/]")


if __name__ == "__main__":
    unittest.main()
