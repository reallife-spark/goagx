#!/usr/bin/env python3
"""Peer-side offline-repair recorder.

An isolated node can be repaired locally. This CLI records what changed, signs
the record with the node's own identity key, and appends it to a 0600 spool. On
reconnect the Master verifies the record and flags it for review; the record is
evidence for that node, never authority and never a grant.

The signed envelope and its refusal rules live in
``shared/sparknet-authz.py`` (`offline-repair`); the policy verifier lives in
``shared/sparknet-sso.py``. This service only gathers the facts and writes the
spool.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROLE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROLE_ROOT.parent / "shared"))

try:
    from sparknet_loader import load_shared
except ImportError as exc:  # pragma: no cover - the bundle always carries the loader
    raise SystemExit(f"cannot load shared/sparknet_loader.py: {exc}") from exc

SPOOL_NAME = "offline-repair.jsonl"


def _load_sso() -> Any:
    return load_shared(
        "sparknet-sso.py",
        "sparknet_sso_offline_repair",
        required=("sparknet-authz.py", "sparknet-sso.py"),
        error=RuntimeError,
        message="cannot find shared/sparknet-sso.py; the recorder carries no policy copy",
    )


def state_dir(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("SPARKNET_STATE_DIR", "").strip()
    if env:
        return Path(env)
    return ROLE_ROOT / ".state"


def spool_path(state: Path) -> Path:
    return state / "admin-events" / SPOOL_NAME


def record(
    *,
    state: Path,
    key_path: Path,
    node_id: str,
    node_fingerprint: str,
    action: str,
    target: dict[str, Any],
    change: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Sign one repair record and append it to the spool. Never executes it."""
    sso = _load_sso()
    if not node_id or not node_fingerprint:
        raise ValueError("node id and fingerprint are required")
    instant = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    payload = {
        "schema": "sparknet-offline-repair/1",
        "repair_id": uuid.uuid4().hex,
        "node_id": node_id,
        "node_fingerprint": node_fingerprint,
        "created_at": instant,
        "action": action,
        "target": target,
        "change": change,
        "authority_effect": "none",
    }
    signed = sso.sign_offline_repair(payload, sso.load_private_key(key_path))
    path = spool_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(signed, sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return signed


def load_spool(state: Path) -> list[dict[str, Any]]:
    path = spool_path(state)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                records.append(row)
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Peer offline-repair recorder (signed evidence, never authority)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir", type=Path, default=None)
    common.add_argument("--json", action="store_true")

    rec = sub.add_parser("record", parents=[common], help="sign and spool one repair record")
    rec.add_argument("--key", type=Path, default=None)
    rec.add_argument("--node-id", default=os.environ.get("SPARKNET_NODE_ID", ""))
    rec.add_argument("--node-fingerprint", default=os.environ.get("SPARKNET_NODE_FINGERPRINT", ""))
    rec.add_argument(
        "--action",
        required=True,
        choices=[
            "user-role",
            "user-approve",
            "kb-change",
            "chat-change",
            "setting-change",
            "local-repair",
        ],
    )
    rec.add_argument("--target", required=True, help="JSON object naming the local target")
    rec.add_argument("--change", required=True, help="JSON object describing the local change")

    listing = sub.add_parser("list", parents=[common], help="print the spooled records")
    listing.add_argument("--limit", type=int, default=50)
    sub.add_parser("check", parents=[common], help="validate the spool shape and signatures presence")
    args = parser.parse_args(argv)
    state = state_dir(getattr(args, "state_dir", None))
    if args.cmd == "record":
        key = args.key or (state / "keys" / "ed25519-node.key.b64")
        try:
            signed = record(
                state=state,
                key_path=key,
                node_id=args.node_id,
                node_fingerprint=args.node_fingerprint,
                action=args.action,
                target=json.loads(args.target),
                change=json.loads(args.change),
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[offline-repair] {exc}", file=sys.stderr)
            return 1
        print(json.dumps(signed, indent=2, sort_keys=True) if args.json else signed["repair_id"])
        return 0
    if args.cmd == "list":
        rows = load_spool(state)[-args.limit :]
        print(
            json.dumps(rows, indent=2, sort_keys=True)
            if args.json
            else "\n".join(f"{row.get('created_at')} {row.get('action')} {row.get('repair_id')}" for row in rows)
        )
        return 0
    rows = load_spool(state)
    bad = [row for row in rows if not row.get("signature_b64") or row.get("authority_effect") != "none"]
    if bad:
        print(f"[offline-repair] {len(bad)} invalid record(s) in {spool_path(state)}", file=sys.stderr)
        return 1
    print(f"[offline-repair] {len(rows)} record(s) spooled at {spool_path(state)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
