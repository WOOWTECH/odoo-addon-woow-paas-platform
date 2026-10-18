"""export-addons.sh 的 tar 指令（scripts/source/lib.sh）＋addons_unpack.py 的端到端測試（#1844）。

模組根目錄本身是 symlink 時，原本只打包到連結本身，解出來 0 個檔、內容 hash 與盤點不符（exit 4）。
這裡用 lib.sh 的同一組函式產生指令、在本機實際跑 GNU tar，確認解出來的內容 hash 與盤點一致，
而且模組內部的 symlink 仍是連結（沒有被展開）。沒有 bash 或 GNU tar 的環境整批 skip。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import addons_unpack  # noqa: E402
from odoo_inspect import dir_digest, scan_addons  # noqa: E402

LIB = SKILL / "scripts" / "source" / "lib.sh"


def gnu_tar():
    tar = shutil.which("tar")
    if not tar or not shutil.which("bash"):
        return False
    out = subprocess.run([tar, "--version"], capture_output=True, text=True)
    return "GNU tar" in out.stdout


def lib_cmd(func, *args):
    """在 bash 裡 source lib.sh 並呼叫函式，回傳指令陣列（函式每個參數印一行）。"""
    out = subprocess.run(["bash", "-c", 'source "$0"; "$@"', str(LIB), func, *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


@unittest.skipUnless(gnu_tar(), "needs bash and GNU tar")
class TestLinkedModuleExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.base = self.dir / "extra-addons"
        real = self.base / "_repo_bridge"
        (real / "models").mkdir(parents=True)
        (real / "__manifest__.py").write_text("{'name': 'linked', 'version': '18.0.1.0'}\n")
        (real / "models" / "x.py").write_text("X = 1\n")
        (real / "inner_link.py").symlink_to("models/x.py")   # 模組內部的連結要照原樣保留
        (real / "up_link.py").symlink_to("./models/x.py")    # 以 . 開頭的目標不能被 transform 改掉
        (real / "__pycache__").mkdir()
        (real / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\0")
        (self.base / "linked_mod").symlink_to("_repo_bridge")
        plain = self.base / "plain_mod"
        plain.mkdir()
        (plain / "__manifest__.py").write_text("{'name': 'plain', 'version': '18.0.1.0'}\n")
        scan = scan_addons([str(self.base)], digest_filter=lambda d: True)
        self.modules = {k: v for k, v in scan["modules"].items() if k in ("linked_mod", "plain_mod")}
        self.inventory = self.dir / "inventory.json"
        self.inventory.write_text(json.dumps({"source": {"namespace": "x-odoo"}, "generated_at": "x",
                                              "inspect": {"addons": {"modules": self.modules}}}))

    def tearDown(self):
        self.tmp.cleanup()

    def run_tar(self, cmd, name):
        path = self.dir / name
        with open(path, "wb") as fh:
            subprocess.run(cmd, stdout=fh, check=True)
        return path

    def test_inventory_marks_the_linked_root(self):
        self.assertEqual(self.modules["linked_mod"]["root_symlink"], "_repo_bridge")
        self.assertNotIn("root_symlink", self.modules["plain_mod"])
        self.assertEqual(self.modules["linked_mod"]["digest"], dir_digest(self.base / "_repo_bridge"))

    def test_plain_tar_of_a_linked_root_is_empty(self):
        """修正前的行為：只打包連結本身 ⇒ 0 個檔、hash 不符（exit 4）。"""
        tar = self.run_tar(lib_cmd("addon_tar_cmd", str(self.base), "linked_mod", "plain_mod"), "old.tar")
        rc = addons_unpack.main(["--inventory", str(self.inventory), "--out", str(self.dir / "old"),
                                 "%s=%s" % (self.base, tar)])
        self.assertEqual(rc, 4)

    def test_linked_root_exports_its_content(self):
        plain = self.run_tar(lib_cmd("addon_tar_cmd", str(self.base), "plain_mod"), "plain.tar")
        linked = self.run_tar(lib_cmd("addon_tar_linked_cmd", str(self.base), "linked_mod"), "linked.tar")
        out = self.dir / "out"
        rc = addons_unpack.main(["--inventory", str(self.inventory), "--out", str(out),
                                 "%s=%s" % (self.base, plain), "%s=%s" % (self.base, linked)])
        self.assertEqual(rc, 0)
        mod = out / "addons" / "linked_mod"
        self.assertFalse(mod.is_symlink(), "the module itself is a real directory after export")
        self.assertTrue((mod / "inner_link.py").is_symlink())
        # 用 os.readlink 看原字串（Path.readlink 會把開頭的 ./ 正規化掉）
        self.assertEqual(os.readlink(mod / "up_link.py"), "./models/x.py")
        self.assertFalse((mod / "__pycache__").exists())
        report = json.loads((out / "addons-export.json").read_text())["modules"]
        self.assertEqual(report["linked_mod"]["sha256"], self.modules["linked_mod"]["digest"]["sha256"])
        self.assertEqual(report["linked_mod"]["root_symlink"], "_repo_bridge")
        self.assertIsNone(report["plain_mod"]["root_symlink"])


if __name__ == "__main__":
    unittest.main()
