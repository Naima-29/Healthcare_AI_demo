"""python -m evals {list | validate <dataset> | run <dataset> --target <name> [--gate]}"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evals import dataset, report, runner, targets

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "reports"


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="evals")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list datasets and targets")
    v = sub.add_parser("validate", help="validate a dataset")
    v.add_argument("dataset")
    r = sub.add_parser("run", help="run a target over a dataset")
    r.add_argument("dataset")
    r.add_argument("--target", required=True)
    r.add_argument("--gate", action="store_true", help="exit 1 if a threshold in the card fails")
    r.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = p.parse_args(argv)

    if a.cmd == "list":
        print("Datasets:")
        for name in dataset.available():
            ds = dataset.load(name)
            print(f"  {name} v{ds.card['version']}  task={ds.task}  items={len(ds.items)}  {ds.card['review_status']}")
        print("Targets:")
        for name, (task, _) in sorted(targets.REGISTRY.items()):
            print(f"  {name}  task={task}")
        return 0

    ds = dataset.load(a.dataset)
    if ds.errors:
        print(f"{ds.name}: {len(ds.errors)} problem(s)")
        for e in ds.errors:
            print(f"  - {e}")
        return 1
    if a.cmd == "validate":
        print(f"{ds.name} v{ds.card['version']}: {len(ds.items)} items, OK")
        return 0

    task, predict = targets.get(a.target)
    if task != ds.task:
        print(f"target '{a.target}' is for task '{task}', dataset '{ds.name}' is '{ds.task}'")
        return 1
    try:
        predict(ds.items[0])
    except targets.TargetUnavailable as e:
        print(f"target '{a.target}' unavailable: {e}")
        return 2
    except Exception:
        pass  # per-item failures are recorded by the runner

    result = runner.run(ds, predict)
    checks = runner.check_thresholds(result, ds.card.get("thresholds", {}))
    path = report.write(ds, a.target, result, a.out)
    m = result["metrics"]
    extra = (f"high-recall={m['high']['recall']:.3f} critical-recall={m['critical_recall'] or 0:.3f} "
             if "high" in m else f"macro-f1={m['macro_f1']:.3f} ")
    print(f"{ds.name} v{ds.card['version']} × {a.target}: n={m['n']} {extra}"
          f"accuracy={m['accuracy']:.3f} errors={m['errors']}")
    for c in checks:
        actual = "n/a" if c["actual"] is None else f"{c['actual']:.3f}"
        print(f"  {'PASS' if c['ok'] else 'FAIL'}  {c['metric']} {actual} (target ≥ {c['target']})")
    print(f"report: {path}")
    if not ds.reviewed:
        print("note: draft labels, not reviewed by the clinical lead")
    return 1 if a.gate and not all(c["ok"] for c in checks) else 0
