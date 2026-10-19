"""addons_unpack.py 的單元測試：python3 -m unittest discover -s .claude/skills/standalone-odoo-migrate/tests"""
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import addons_unpack  # noqa: E402
from odoo_inspect import dir_digest  # noqa: E402

BASE = "/mnt/extra-addons"


class TestAddonsUnpack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "src"
        for name in ("mod_a", "mod_b"):
            (self.src / name / "models").mkdir(parents=True)
            (self.src / name / "__manifest__.py").write_text("{'name': '%s', 'version': '1.0'}\n" % name)
            (self.src / name / "models" / "x.py").write_text("X = '%s'\n" % name)
        (self.src / "mod_a" / "link.py").symlink_to("models/x.py")
        self.inventory = self.dir / "inventory.json"
        self.write_inventory()

    def tearDown(self):
        self.tmp.cleanup()

    def write_inventory(self, **override):
        modules = {name: {"dir": BASE, "version": "18.0.1.0", "digest": dir_digest(self.src / name), "git": None}
                   for name in ("mod_a", "mod_b")}
        modules["base"] = {"dir": "/usr/lib/python3/dist-packages/odoo/addons", "version": "18.0.1.3"}
        modules.update(override)
        self.inventory.write_text(json.dumps({
            "source": {"namespace": "x-odoo"}, "generated_at": "20261010T000000Z",
            "inspect": {"addons": {"modules": modules}}}))

    def make_tar(self, names=("mod_a", "mod_b"), extra=None):
        path = self.dir / "a.tar"
        with tarfile.open(path, "w") as tar:
            for name in names:
                tar.add(self.src / name, arcname=name)
            for arcname, data in (extra or {}).items():
                info = tarfile.TarInfo(arcname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return path

    def run_unpack(self, tar, out="out", modules=None):
        args = ["--inventory", str(self.inventory), "--out", str(self.dir / out)]
        if modules:
            args += ["--modules", modules]
        return addons_unpack.main(args + ["%s=%s" % (BASE, tar)])

    def test_roundtrip_verifies_digest_and_writes_report(self):
        self.assertEqual(self.run_unpack(self.make_tar()), 0)
        out = self.dir / "out"
        self.assertTrue((out / "addons" / "mod_a" / "link.py").is_symlink())
        report = json.loads((out / "addons-export.json").read_text())
        self.assertEqual(sorted(report["modules"]), ["mod_a", "mod_b"])
        self.assertEqual(report["modules"]["mod_b"]["sha256"], dir_digest(self.src / "mod_b")["sha256"])

    def test_modules_filter(self):
        self.assertEqual(self.run_unpack(self.make_tar(names=("mod_b",)), modules="mod_b"), 0)
        self.assertFalse((self.dir / "out" / "addons" / "mod_a").exists())
        self.assertEqual(self.run_unpack(self.make_tar(), out="out2", modules="nope"), 2)

    def test_truncated_or_incomplete_tar_fails(self):
        tar = self.make_tar()
        data = tar.read_bytes()
        tar.write_bytes(data[:len(data) // 2])
        self.assertEqual(self.run_unpack(tar), 3)
        # tar 完整但少一個模組
        self.assertEqual(self.run_unpack(self.make_tar(names=("mod_a",)), out="out2"), 3)

    def test_content_changed_since_inventory_fails(self):
        tar = self.make_tar()
        (self.src / "mod_b" / "models" / "x.py").write_text("X = 'changed'\n")
        self.write_inventory()  # 盤點記的是改過的版本，tar 是舊的
        self.assertEqual(self.run_unpack(tar), 4)
        self.assertFalse((self.dir / "out" / "addons-export.json").exists())

    def test_unsafe_members_rejected(self):
        self.assertEqual(self.run_unpack(self.make_tar(extra={"../evil.py": b"x"})), 3)
        self.assertEqual(self.run_unpack(self.make_tar(extra={"other_mod/a.py": b"x"}), out="out2"), 3)
        # 成員路徑經過 symlink（想藉連結寫到目的地以外）⇒ 拒收，連結本身也不會先被建立
        tar = self.dir / "link.tar"
        with tarfile.open(tar, "w") as archive:
            archive.add(self.src / "mod_a", arcname="mod_a")
            archive.add(self.src / "mod_b", arcname="mod_b")
            info = tarfile.TarInfo("mod_a/escape")
            info.type = tarfile.SYMTYPE
            info.linkname = str(self.dir / "victim")
            archive.addfile(info)
            payload = tarfile.TarInfo("mod_a/escape/pwned.py")
            payload.size = 1
            archive.addfile(payload, io.BytesIO(b"x"))
        self.assertEqual(self.run_unpack(tar, out="out3"), 3)
        self.assertFalse((self.dir / "victim").exists())
        hard = self.dir / "hard.tar"
        with tarfile.open(hard, "w") as archive:
            archive.add(self.src / "mod_a", arcname="mod_a")
            archive.add(self.src / "mod_b", arcname="mod_b")
            info = tarfile.TarInfo("mod_a/hard")
            info.type = tarfile.LNKTYPE
            info.linkname = "mod_b/__manifest__.py"
            archive.addfile(info)
        self.assertEqual(self.run_unpack(hard, out="out4"), 3)

    def test_symlink_pointing_outside_is_kept_and_reported(self):
        # Odoo 內建 point_of_sale 就有指向系統字型的 symlink；內容 hash 計入連結目標，照樣保留並列入報告
        (self.src / "mod_b" / "font.otf").symlink_to("../../../share/fonts/x.otf")
        self.write_inventory()
        self.assertEqual(self.run_unpack(self.make_tar()), 0)
        link = self.dir / "out" / "addons" / "mod_b" / "font.otf"
        self.assertTrue(link.is_symlink())
        report = json.loads((self.dir / "out" / "addons-export.json").read_text())
        self.assertEqual(report["modules"]["mod_b"]["symlinks_outside"],
                         [{"path": "mod_b/font.otf", "target": "../../../share/fonts/x.otf"}])
        self.assertEqual(report["modules"]["mod_a"]["symlinks_outside"], [])

    def test_several_tars_for_one_addon_dir(self):
        """同一個 addon 根目錄可以分好幾顆 tar（根目錄是 symlink 的模組各自一顆，#1844）。"""
        tar_a, tar_b = self.dir / "a1.tar", self.dir / "b1.tar"
        with tarfile.open(tar_a, "w") as tar:
            tar.add(self.src / "mod_a", arcname="mod_a")
        with tarfile.open(tar_b, "w") as tar:
            tar.add(self.src / "mod_b", arcname="mod_b")
        args = ["--inventory", str(self.inventory), "--out", str(self.dir / "out")]
        self.assertEqual(addons_unpack.main(args + ["%s=%s" % (BASE, tar_a), "%s=%s" % (BASE, tar_b)]), 0)
        # 同一個模組出現在兩顆 tar ⇒ 拒收
        both = self.make_tar()
        args2 = ["--inventory", str(self.inventory), "--out", str(self.dir / "out2")]
        self.assertEqual(addons_unpack.main(args2 + ["%s=%s" % (BASE, both), "%s=%s" % (BASE, tar_b)]), 3)
        # 少一個模組 ⇒ 拒收
        args3 = ["--inventory", str(self.inventory), "--out", str(self.dir / "out3")]
        self.assertEqual(addons_unpack.main(args3 + ["%s=%s" % (BASE, tar_a)]), 3)

    def test_refuses_non_empty_output(self):
        (self.dir / "out" / "addons" / "old").mkdir(parents=True)
        self.assertEqual(self.run_unpack(self.make_tar()), 2)


if __name__ == "__main__":
    unittest.main()
