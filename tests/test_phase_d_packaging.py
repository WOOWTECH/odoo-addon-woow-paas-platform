"""TDD red phase — Phase D F-PC-1 (A-1): websockets requirement upper bound.

Plan : docs/plans/2026-Q2/agent-runtime-pod-plan.md WS1 F-PC-1 (caveat A-1).
Bug  : ``requirements.txt:10`` pins ``websockets>=12.0`` with NO upper bound.
       websockets >= 14 RENAMED the ``extra_headers`` kwarg to
       ``additional_headers`` — the SSE→pod WS bridge at
       ``src/controllers/ai_assistant.py:648-650`` calls
       ``websockets.connect(... extra_headers=...)``, so a host that installs
       websockets >= 14 breaks the bridge at runtime (TypeError).

Fix direction: pin an upper bound ``websockets>=12.0,<14`` (and, optionally,
pick the kwarg by version at the connect site — that compat shim is verified
on a live pod at the pilot gate, NOT unit-testable here).

WHY this is a HOST-side test (not the in-container Odoo suite): the docker
dev/test setup mounts only ``./src`` at ``/mnt/extra-addons/woow_paas_platform``
(see docker-compose.yml). The websockets pin lives in the FLOOR-root
``requirements.txt``, which is NOT inside that mount, so an in-container Odoo
test cannot read it. Run on the host:

    python3 -m unittest tests.test_phase_d_packaging -v
    # or:  python3 tests/test_phase_d_packaging.py

Red expectation today (fix NOT started): the pin has no ``<14`` → the upper
bound assertion fails. After the fix it parses to ``>=12,<14`` → green.

Boundary (intentionally NOT here):
* The runtime connect-kwarg compat (extra_headers vs additional_headers) on a
  real websockets>=14 install → pilot-gate / live-pod verification (no pod =
  no unit test, per plan §Test scope "刻意不測").
"""
import os
import re
import unittest


def _floor_root():
    # this file lives at <floor>/tests/, so the floor root is one level up.
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestWebsocketsRequirementPin(unittest.TestCase):
    """F-PC-1 — websockets must be pinned with a ``<14`` upper bound."""

    def setUp(self):
        self.req_path = os.path.join(_floor_root(), "requirements.txt")
        self.assertTrue(
            os.path.isfile(self.req_path),
            "fixture: floor-root requirements.txt not found at %s (run this "
            "test on the HOST, not inside the docker Odoo container)."
            % self.req_path,
        )
        with open(self.req_path, "r", encoding="utf-8") as fh:
            self.lines = [
                ln.strip() for ln in fh
                if ln.strip() and not ln.strip().startswith("#")
            ]

    def _websockets_specifier(self):
        for ln in self.lines:
            if re.match(r"^websockets\b", ln):
                # strip an inline comment if present
                return ln.split("#", 1)[0].strip()
        self.fail(
            "F-PC-1 fixture: no 'websockets' requirement line found in %s."
            % self.req_path
        )

    def test_websockets_has_upper_bound_below_14(self):
        spec = self._websockets_specifier()
        # Must declare an exclusive upper bound that keeps websockets < 14
        # (e.g. '<14', '<14.0', '<13', or '==12.*'). A bare '>=12.0' is the
        # live bug: it permits 14+ which renamed extra_headers.
        upper = re.search(r"<\s*(\d+)(?:\.\d+)*", spec)
        self.assertIsNotNone(
            upper,
            "F-PC-1: websockets requirement '%s' has NO upper bound — "
            "websockets>=14 renamed connect(extra_headers=) to "
            "additional_headers and breaks the WS bridge "
            "(ai_assistant.py:648-650). Pin 'websockets>=12.0,<14'." % spec,
        )
        self.assertLessEqual(
            int(upper.group(1)), 14,
            "F-PC-1: websockets upper bound must keep the resolved version "
            "below 14 (got '%s')." % spec,
        )


if __name__ == "__main__":
    unittest.main()
