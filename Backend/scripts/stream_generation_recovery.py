#!/usr/bin/env python3
"""Non-executing administration. Input is a cTrader CLOSED-history export, not a broker connection."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import SessionLocal
from stream_generations import plan, apply, snapshot, digest, GenerationBlocked


def save_private(path, data):
    path = Path(path)
    # Do not replace an existing artifact accidentally; operator chooses fresh names.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--history", required=True, help="Scoped closed cTrader history JSON export"
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    p.add_argument(
        "--plan-file", required=True, help="Dry-run output / approved plan input"
    )
    p.add_argument(
        "--snapshot-file",
        required=True,
        help="Dry-run predecessor snapshot output / apply input",
    )
    args = p.parse_args(argv)
    history = json.loads(Path(args.history).read_text())
    try:
        if args.apply:
            approved = json.loads(Path(args.plan_file).read_text())
            before = json.loads(Path(args.snapshot_file).read_text())
            if digest(before) != approved.get("snapshot_hash"):
                raise GenerationBlocked("recorded predecessor snapshot mismatch")
            result = apply(SessionLocal, history, approved_plan=approved)
        else:
            result = plan(SessionLocal, history)
            if result["verdict"] == "SAFE_TO_CREATE_NEW_GENERATION":
                with SessionLocal() as s:
                    before = snapshot(s, result["old_key"], result["timeframe"])
                if digest(before) != result["snapshot_hash"]:
                    raise GenerationBlocked("stream changed during dry-run snapshot")
                save_private(args.snapshot_file, before)
                save_private(args.plan_file, result)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0 if result.get("verdict") != "BLOCKED" else 2
    except (GenerationBlocked, ValueError, KeyError, OSError) as e:
        print(json.dumps({"verdict": "BLOCKED", "reason": str(e)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
