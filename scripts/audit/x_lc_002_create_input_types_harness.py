#!/usr/bin/env python3
"""Run X-LC-002's actual _create_service function through red/green type probes.

Run both sides with:
    python3 scripts/audit/x_lc_002_create_input_types_harness.py

The red side loads the pre-fix controller from c14d5988's parent; the green side
loads the checked-out controller. No Odoo installation, database, or network is
used. ORM sentinels prove malformed values stop before any browse/capability call.
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import subprocess
import sys
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[2]
CONTROLLER = pathlib.Path("src/controllers/paas.py")
BASELINE_REF = "c14d5988^"
INVALID_REQUEST = {"success": False, "error": "Invalid request"}

INVALID_CASES = (
    ("name integer", 18, 123, 24),
    ("name float", 18, 1.5, 24),
    ("name array", 18, ["service"], 24),
    ("name object", 18, {"name": "service"}, 24),
    ("name boolean", 18, True, 24),
    ("template string", "18", "service", 24),
    ("template boolean", True, "service", 24),
    ("template array", [18], "service", 24),
    ("template object", {"id": 18}, "service", 24),
    ("template float", 18.5, "service", 24),
    ("plan string", 18, "service", "24"),
    ("plan boolean", 18, "service", True),
    ("plan array", 18, "service", [24]),
    ("plan object", 18, "service", {"id": 24}),
    ("plan float", 18, "service", 24.5),
    ("numeric strings", "18", "service", "24"),
)

MISSING_CASES = (
    ("missing template", None, "service", 24, "Template ID is required"),
    ("missing name", 18, None, 24, "Service name is required"),
    ("missing plan", 18, "service", None, "Subscription plan is required"),
)


class ORMReached(Exception):
    """Raised by the test doubles if input validation reaches an ORM boundary."""


class FakePlan:
    pass


class FakeTemplate:
    is_active = True
    is_hidden = False
    subscription_plan_ids = (FakePlan(),)

    def exists(self):
        return True

    def sudo(self):
        return self


class TemplateModel:
    def browse(self, value):
        if type(value) is int:
            return FakeTemplate()
        raise ORMReached(f"cloud template browse received {type(value).__name__}")


class PlanModel:
    def with_context(self, _context):
        return self

    def sudo(self):
        return self

    def browse(self, value):
        raise ORMReached(f"plan browse received {type(value).__name__}")


class FakeRequest:
    env = {"sh.subscription.plan": PlanModel()}


class FakeWorkspace:
    env = {"woow_paas_platform.cloud_service": object()}


def _extract_method(source: str):
    tree = ast.parse(source)
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_create_service"
    )
    namespace = {"Any": Any, "_": lambda message: message}
    module = ast.Module(body=[method], type_ignores=[])
    exec(compile(module, str(CONTROLLER), "exec"), namespace)
    return namespace["_create_service"]


def _current_source() -> str:
    return (ROOT / CONTROLLER).read_text(encoding="utf-8")


def _baseline_source() -> str:
    return subprocess.run(
        ["git", "show", f"{BASELINE_REF}:{CONTROLLER.as_posix()}"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout


def _run_invalid_cases(method, mode: str) -> int:
    failures = 0
    for label, template_id, name, plan_id in INVALID_CASES:
        globals_for_method = method.__globals__
        globals_for_method["request"] = FakeRequest()
        calls = []

        class Capability:
            def __init__(self, _env):
                calls.append("capability")

            def cloud_templates(self):
                return TemplateModel()

        globals_for_method["_PaaSCapability"] = Capability
        try:
            result = method(
                None, FakeWorkspace(), template_id, name, {}, plan_id=plan_id,
            )
            error = None
        except Exception as exc:  # baseline must expose the old crash/ORM path
            result = None
            error = f"{type(exc).__name__}: {exc}"

        if mode == "red":
            passed = result != INVALID_REQUEST
            detail = error or f"returned {result!r}"
            label_out = "RED reproduced" if passed else "UNEXPECTED green"
        else:
            passed = result == INVALID_REQUEST and not calls
            detail = error or f"returned {result!r}; capability calls={len(calls)}"
            label_out = "GREEN rejected before ORM" if passed else "FAIL"

        print(f"{label_out}: {label}: {detail}")
        failures += not passed
    return failures


def _run_missing_cases(method) -> int:
    failures = 0
    for label, template_id, name, plan_id, message in MISSING_CASES:
        method.__globals__["request"] = FakeRequest()

        class Capability:
            def __init__(self, _env):
                pass

            def cloud_templates(self):
                return TemplateModel()

        method.__globals__["_PaaSCapability"] = Capability
        try:
            result = method(
                None, FakeWorkspace(), template_id, name, {}, plan_id=plan_id,
            )
            passed = result == {"success": False, "error": message}
        except Exception as exc:
            result = f"{type(exc).__name__}: {exc}"
            passed = False
        print(f"{'PASS' if passed else 'FAIL'}: {label}: {result!r}")
        failures += not passed
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--red", action="store_true", help="run pre-fix source only")
    group.add_argument("--green", action="store_true", help="run working-tree source only")
    args = parser.parse_args()
    modes = ["red"] if args.red else ["green"] if args.green else ["red", "green"]
    failures = 0
    for mode in modes:
        source = _baseline_source() if mode == "red" else _current_source()
        method = _extract_method(source)
        print(f"=== {mode.upper()} ({BASELINE_REF if mode == 'red' else 'working tree'}) ===")
        failures += _run_invalid_cases(method, mode)
        failures += _run_missing_cases(method)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
