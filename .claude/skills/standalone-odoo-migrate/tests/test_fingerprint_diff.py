"""fingerprint_diff.py 的單元測試（不需要叢集）。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fingerprint_diff  # noqa: E402

SRC_FP = """meta|server_major|17
ext|plpgsql|1.0
ext|vector|0.8.0
seq|res_partner_id_seq|210
seq|ir_ui_view_id_seq|900
table|res_partner|210|aaa
table|fake_embedding|25|bbb
table|ir_ui_view|900|ccc
"""


class TestFingerprintDiff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text)
        return str(path)

    def run_diff(self, dst_fp, stage="restore", src_fs=None, dst_fs=None):
        args = ["--source-db", self.write("src.fp", SRC_FP), "--target-db", self.write("dst.fp", dst_fp),
                "--stage", stage]
        if src_fs is not None:
            args += ["--source-fs", self.write("src.fs", src_fs), "--target-fs", self.write("dst.fs", dst_fs)]
        return fingerprint_diff.main(args)

    def test_identical_passes_and_extension_version_is_informational(self):
        self.assertEqual(self.run_diff(SRC_FP.replace("vector|0.8.0", "vector|0.8.1")), 0)

    def test_restore_stage_is_exact(self):
        self.assertEqual(self.run_diff(SRC_FP.replace("ir_ui_view|900|ccc", "ir_ui_view|900|zzz")), 1)
        self.assertEqual(self.run_diff(SRC_FP.replace("res_partner_id_seq|210", "res_partner_id_seq|1")), 1)

    def test_upgrade_stage_tolerates_metadata_only(self):
        changed_meta = SRC_FP.replace("ir_ui_view|900|ccc", "ir_ui_view|950|zzz").replace(
            "ir_ui_view_id_seq|900", "ir_ui_view_id_seq|950")
        self.assertEqual(self.run_diff(changed_meta, stage="upgrade"), 0)
        self.assertEqual(self.run_diff(SRC_FP.replace("fake_embedding|25|bbb", "fake_embedding|24|bbb"),
                                       stage="upgrade"), 1)

    def test_filestore(self):
        src_fs = "h1  ab/ab12\nh2  cd/cd34\n"
        self.assertEqual(self.run_diff(SRC_FP, src_fs=src_fs, dst_fs=src_fs), 0)
        self.assertEqual(self.run_diff(SRC_FP, src_fs=src_fs, dst_fs="h1  ab/ab12\n"), 1)
        self.assertEqual(self.run_diff(SRC_FP, src_fs=src_fs, dst_fs="h1  ab/ab12\nhX  cd/cd34\n"), 1)
        extra = src_fs + "h3  ef/ef56\n"
        self.assertEqual(self.run_diff(SRC_FP, src_fs=src_fs, dst_fs=extra), 1)
        self.assertEqual(self.run_diff(SRC_FP, stage="upgrade", src_fs=src_fs, dst_fs=extra), 0)


if __name__ == "__main__":
    unittest.main()
