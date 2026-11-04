# SPDX-FileCopyrightText: 2026 Francisco Angulo de Lafuente
# SPDX-License-Identifier: MIT
# Research-Project: RSNA Knee MRI Submission Audit
# Research-Date: 2026-11-04
# Research-ORCID: https://orcid.org/0009-0001-1634-7063
# Reuse: Keep the MIT copyright and permission notice; please cite the author.
"""Static release gate for Kaggle code-competition submission notebooks.

This catches a common failure mode: blending cached `submission.csv` files
from Kaggle's three-row public example instead of predicting on the hidden
test set at scoring time. A PASS is only a source-level check, never proof of
runtime correctness, model quality, or leaderboard improvement.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any


def _path_value(node: ast.AST, env: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.replace("\\", "/")
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.Call):
        name = node.func.id if isinstance(node.func, ast.Name) else (
            node.func.attr if isinstance(node.func, ast.Attribute) else ""
        )
        if name in {"Path", "PurePath", "PurePosixPath"} and node.args:
            return _path_value(node.args[0], env)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _path_value(node.left, env)
        right = _path_value(node.right, env)
        if left is not None and right is not None:
            return str(PurePosixPath(left) / right)
    return None


def _calls(cells: list[str]) -> tuple[list[ast.Call], list[str], list[str]]:
    calls: list[ast.Call] = []
    static_input_submissions: set[str] = set()
    parse_errors: list[str] = []

    for cell_index, source in enumerate(cells):
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            parse_errors.append(f"cell {cell_index}: {exc.msg}")
            continue

        env: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                value = _path_value(node.value, env)
                if value is not None:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            env[target.id] = value
                    if (value.startswith("/kaggle/input/")
                            and PurePosixPath(value).name.lower() == "submission.csv"):
                        static_input_submissions.add(value)

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value.replace("\\", "/")
                if (value.startswith("/kaggle/input/")
                        and PurePosixPath(value).name.lower() == "submission.csv"):
                    static_input_submissions.add(value)
            if isinstance(node, ast.Call):
                calls.append(node)

    return calls, sorted(static_input_submissions), parse_errors


def _contains_string(node: ast.AST, wanted: str) -> bool:
    return any(
        isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and child.value.replace("\\", "/").lower().endswith(wanted.lower())
        for child in ast.walk(node)
    )


def audit_source(source: str, source_sha256: str) -> dict[str, Any]:
    cells = [source]
    calls, static_paths, parse_errors = _calls(cells)

    def read_csv_calls_ending(token: str) -> bool:
        for call in calls:
            function = call.func
            name = function.id if isinstance(function, ast.Name) else (
                function.attr if isinstance(function, ast.Attribute) else ""
            )
            if name == "read_csv" and call.args and _contains_string(call.args[0], token):
                return True
        return False

    has_csv_writer = any(
        (call.func.attr if isinstance(call.func, ast.Attribute) else "") == "to_csv"
        for call in calls
    )
    has_uid_reference = "StudyInstanceUID" in source
    checks = {
        "no_precomputed_submission_csv_from_kaggle_input": not static_paths,
        "reads_live_test_csv": read_csv_calls_ending("test.csv"),
        "reads_sample_submission": read_csv_calls_ending("sample_submission.csv"),
        "writes_csv": has_csv_writer,
        "references_study_uid": has_uid_reference,
        "all_code_cells_parse": not parse_errors,
    }
    return {
        "status": "STATIC_SOURCE_PASS" if all(checks.values()) else "STATIC_SOURCE_REJECT",
        "scope": "source-only; does not prove Kaggle hidden-run identity, runtime success, accuracy, or score",
        "source_sha256": source_sha256,
        "checks": checks,
        "detected_static_input_submission_paths": static_paths,
        "parse_errors": parse_errors,
    }


def audit_notebook(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    notebook = json.loads(raw.decode("utf-8"))
    cells: list[str] = []
    for cell in notebook.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        source = cell.get("source", "")
        cells.append("".join(source) if isinstance(source, list) else str(source))
    report = audit_source("\n".join(cells), hashlib.sha256(raw).hexdigest())
    report["notebook"] = path.name
    report["code_cells"] = len(cells)
    return report


def self_test() -> None:
    import tempfile

    static_example = """from pathlib import Path
import pandas as pd
ROOT = Path('/kaggle/input')
V1 = ROOT / 'old-run' / 'submission.csv'
frame = pd.read_csv(V1)
frame.to_csv('/kaggle/working/submission.csv', index=False)
"""
    dynamic_example = """from pathlib import Path
import pandas as pd
ROOT = Path('/kaggle/input/competition')
test = pd.read_csv(ROOT / 'test.csv')
sample = pd.read_csv(ROOT / 'sample_submission.csv')
assert 'StudyInstanceUID' in sample
sample.to_csv('/kaggle/working/submission.csv', index=False)
"""
    rejected = audit_source(static_example, "synthetic")
    passed = audit_source(dynamic_example, "synthetic")
    assert rejected["status"] == "STATIC_SOURCE_REJECT", rejected
    assert passed["status"] == "STATIC_SOURCE_PASS", passed
    assert rejected["checks"]["no_precomputed_submission_csv_from_kaggle_input"] is False
    assert passed["checks"]["no_precomputed_submission_csv_from_kaggle_input"] is True
    print("SELF_TEST_PASS: static-example source rejected; dynamic-test source accepted")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("notebook", nargs="?", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.notebook is None:
        parser.error("notebook is required unless --self-test is used")
    report = audit_notebook(args.notebook)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["status"] == "STATIC_SOURCE_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
