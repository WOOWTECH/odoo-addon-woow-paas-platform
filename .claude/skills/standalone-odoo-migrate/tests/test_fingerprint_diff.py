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

BASE_BFP = """bcol|res_partner|id|1
bcol|res_partner|phone|2
bmax|res_partner|210
bmax|fake_embedding|25
bmax|ir_cron|14
btable|res_partner|200|p1
btable|fake_embedding|25|e1
btable|ir_cron|14|c1
btable|res_partner_tag_rel|3|r1
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

    def run_diff(self, dst_fp, stage="restore", src_fs=None, dst_fs=None, refs=None):
        args = ["--source-db", self.write("src.fp", SRC_FP), "--target-db", self.write("dst.fp", dst_fp),
                "--stage", stage]
        if src_fs is not None:
            args += ["--source-fs", self.write("src.fs", src_fs), "--target-fs", self.write("dst.fs", dst_fs)]
        if refs is not None:
            args += ["--target-refs", self.write("dst.refs", refs)]
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

    def run_business(self, dst_fp, base_bfp, cur_bfp):
        return fingerprint_diff.main([
            "--source-db", self.write("src.fp", SRC_FP), "--target-db", self.write("dst.fp", dst_fp),
            "--source-business", self.write("base.bfp", base_bfp), "--target-business", self.write("cur.bfp", cur_bfp),
            "--stage", "upgrade"])

    def test_upgrade_business_rows_decide(self):
        # 整表 hash 變了（模組資料列的 write_date 被重寫），但客戶資料列相同 ⇒ 通過
        full_changed = SRC_FP.replace("res_partner|210|aaa", "res_partner|212|zzz").replace(
            "res_partner_id_seq|210", "res_partner_id_seq|212")
        self.assertEqual(self.run_business(full_changed, BASE_BFP, BASE_BFP + "badded|res_partner|2\n"), 0)
        # 客戶資料列內容變了、少了一列、欄不見了 ⇒ 不通過
        self.assertEqual(self.run_business(SRC_FP, BASE_BFP, BASE_BFP.replace("res_partner|200|p1", "res_partner|200|p2")), 1)
        self.assertEqual(self.run_business(SRC_FP, BASE_BFP, BASE_BFP.replace("fake_embedding|25|e1", "fake_embedding|24|e2")), 1)
        self.assertEqual(self.run_business(SRC_FP, BASE_BFP, BASE_BFP + "bmissing|res_partner|phone\n"), 1)
        self.assertEqual(self.run_business(SRC_FP, BASE_BFP, BASE_BFP.replace("btable|fake_embedding|25|e1\n", "")), 1)

    def test_upgrade_business_tolerates_registry_and_idless_growth(self):
        cur = BASE_BFP.replace("ir_cron|14|c1", "ir_cron|14|c2").replace("res_partner_tag_rel|3|r1", "res_partner_tag_rel|4|r2")
        self.assertEqual(self.run_business(SRC_FP, BASE_BFP, cur), 0)
        # 沒有 id 的關聯表：列數沒增加卻內容不同 ⇒ 舊列被動過
        self.assertEqual(self.run_business(SRC_FP, BASE_BFP, BASE_BFP.replace("res_partner_tag_rel|3|r1", "res_partner_tag_rel|3|r2")), 1)

    def test_upgrade_business_tolerates_runtime_and_transient_tables(self):
        base = BASE_BFP + "btable|res_device_log|658|d1\nbtable|mail_compose_message|4|w1\nbtable|sale_order|9|s1\n"
        cur = base.replace("res_device_log|658|d1", "res_device_log|658|d2").replace(
            "mail_compose_message|4|w1", "mail_compose_message|0|-")
        # res_device_log 是執行期表；mail_compose_message 沒有 btransient 行時仍算客戶資料
        self.assertEqual(self.run_business(SRC_FP, base, cur), 1)
        self.assertEqual(self.run_business(SRC_FP, base, cur + "btransient|mail_compose_message\n"), 0)
        # 一般的表不受影響
        self.assertEqual(self.run_business(SRC_FP, base, cur.replace("sale_order|9|s1", "sale_order|8|s2")
                                           + "btransient|mail_compose_message\n"), 1)

    def test_upgrade_business_sequences_only_move_forward(self):
        self.assertEqual(self.run_business(SRC_FP.replace("res_partner_id_seq|210", "res_partner_id_seq|211"), BASE_BFP, BASE_BFP), 0)
        self.assertEqual(self.run_business(SRC_FP.replace("res_partner_id_seq|210", "res_partner_id_seq|5"), BASE_BFP, BASE_BFP), 1)

    def test_business_args_validation(self):
        base = ["--source-db", self.write("src.fp", SRC_FP), "--target-db", self.write("dst.fp", SRC_FP)]
        bfp = self.write("base.bfp", BASE_BFP)
        self.assertEqual(fingerprint_diff.main(base + ["--stage", "upgrade", "--source-business", bfp]), 2)
        self.assertEqual(fingerprint_diff.main(base + ["--stage", "restore", "--source-business", bfp, "--target-business", bfp]), 2)

    def test_filestore(self):
        src_fs = "h1  ab/ab12\nh2  cd/cd34\n"
        self.assertEqual(self.run_diff(SRC_FP, src_fs=src_fs, dst_fs=src_fs), 0)
        self.assertEqual(self.run_diff(SRC_FP, src_fs=src_fs, dst_fs="h1  ab/ab12\n"), 1)
        self.assertEqual(self.run_diff(SRC_FP, src_fs=src_fs, dst_fs="h1  ab/ab12\nhX  cd/cd34\n"), 1)
        extra = src_fs + "h3  ef/ef56\n"
        self.assertEqual(self.run_diff(SRC_FP, src_fs=src_fs, dst_fs=extra), 1)
        self.assertEqual(self.run_diff(SRC_FP, stage="upgrade", src_fs=src_fs, dst_fs=extra), 0)

    def test_filestore_upgrade_with_refs(self):
        src_fs = "h1  ab/ab12\nh2  cd/cd34\n"
        # 模組附件換了內容：舊檔 cd/cd34 沒人引用、被回收，新檔 ef/ef56 被引用 ⇒ 通過
        swapped = "h1  ab/ab12\nh3  ef/ef56\n"
        self.assertEqual(self.run_diff(SRC_FP, stage="upgrade", src_fs=src_fs, dst_fs=swapped, refs="ab/ab12\nef/ef56\n"), 0)
        # 少掉的檔仍被附件引用 ⇒ 不通過
        self.assertEqual(self.run_diff(SRC_FP, stage="upgrade", src_fs=src_fs, dst_fs=swapped, refs="ab/ab12\ncd/cd34\n"), 1)
        # 新附件引用的檔不在 filestore ⇒ 不通過
        self.assertEqual(self.run_diff(SRC_FP, stage="upgrade", src_fs=src_fs, dst_fs=src_fs, refs="ab/ab12\nzz/zz99\n"), 1)
        # 沒給 refs：少任何檔都不通過（舊行為）；refs 只能用在 upgrade 階段
        self.assertEqual(self.run_diff(SRC_FP, stage="upgrade", src_fs=src_fs, dst_fs=swapped), 1)
        self.assertEqual(self.run_diff(SRC_FP, stage="restore", src_fs=src_fs, dst_fs=src_fs, refs="ab/ab12\n"), 2)


if __name__ == "__main__":
    unittest.main()
