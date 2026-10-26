"""Regression guard for Secret copies used by the disposable dev fixtures."""

import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]
DEV_SCRIPTS = (
    SKILL / "scripts" / "dev" / "fake-source.sh",
    SKILL / "scripts" / "dev" / "fake-target.sh",
)


class TestSecretApplySafety(unittest.TestCase):
    def test_secret_copy_is_create_only(self):
        for script in DEV_SCRIPTS:
            with self.subTest(script=script.name):
                source = script.read_text(encoding="utf-8")
                self.assertIn('| "${K[@]}" create -f -', source)
                self.assertNotIn('| "${K[@]}" apply -f -', source)
                self.assertIn("last-applied-configuration", source)


if __name__ == "__main__":
    unittest.main()
