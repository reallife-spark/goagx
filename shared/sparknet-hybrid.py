#!/usr/bin/env python3
"""Semantic + agentic retrieval as one explicit bundle.

The two result sets are different evidence. The bundle returns both, with
provenance, and never silently merges them into a single answer. It is data for
the agent, not an answer and not authority.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

BUNDLE_SCHEMA = "sparknet-hybrid/1"
SEMANTIC_SCHEMA = "sparknet-hybrid-semantic/1"
AGENTIC_SCHEMA = "sparknet-hybrid-agentic/1"
CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
_RANK = {name: index for index, name in enumerate(CLASSIFICATIONS)}


class HybridError(Exception):
    """A fail-closed bundle refusal."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise HybridError(code, message)


def _nonempty(value: Any, field: str, code: str = "bad-result") -> str:
    _require(isinstance(value, str) and bool(value.strip()), code, f"{field} must be a non-empty string")
    return value.strip()


def _classification(value: Any) -> str:
    _require(
        isinstance(value, str) and value in CLASSIFICATIONS,
        "unknown-classification",
        "classification is not in the closed vocabulary",
    )
    return value


def _provenance(value: Any) -> dict[str, Any]:
    _require(isinstance(value, Mapping), "missing-provenance", "provenance must be an object")
    source = _nonempty(value.get("source"), "provenance.source", "missing-provenance")
    digest = value.get("digest")
    _require(
        digest is None or isinstance(digest, str), "missing-provenance", "provenance.digest must be a string or null"
    )
    return {"source": source, "digest": digest}


def _json_value(value: Any, field: str) -> Any:
    try:
        json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise HybridError("bad-result", f"{field} is not JSON serialisable") from exc
    return value


def semantic_result(
    *,
    result_id: str,
    source: str,
    path: str,
    score: float,
    text: str,
    provenance: Mapping[str, Any],
    classification: str = "internal",
) -> dict[str, Any]:
    _require(
        isinstance(score, (int, float)) and not isinstance(score, bool), "bad-score", "score must be a finite number"
    )
    return {
        "schema_version": SEMANTIC_SCHEMA,
        "result_id": _nonempty(result_id, "result_id"),
        "source": _nonempty(source, "source"),
        "path": _nonempty(path, "path"),
        "score": float(score),
        "text": str(text),
        "classification": _classification(classification),
        "provenance": _provenance(provenance),
    }


def agentic_result(
    *,
    result_id: str,
    tool: str,
    action: str,
    result: Any,
    provenance: Mapping[str, Any],
    classification: str = "internal",
) -> dict[str, Any]:
    return {
        "schema_version": AGENTIC_SCHEMA,
        "result_id": _nonempty(result_id, "result_id"),
        "tool": _nonempty(tool, "tool"),
        "action": _nonempty(action, "action"),
        "result": _json_value(result, "result"),
        "classification": _classification(classification),
        "provenance": _provenance(provenance),
    }


def bundle(
    *,
    query: str,
    semantic: Sequence[Mapping[str, Any]],
    agentic: Sequence[Mapping[str, Any]],
    classification: str | None = None,
    department: str | None = None,
    vault_id: str = "",
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Return both result sets; the classification is the highest of any result."""
    _require(isinstance(semantic, (list, tuple)), "malformed-bundle", "semantic must be a list")
    _require(isinstance(agentic, (list, tuple)), "malformed-bundle", "agentic must be a list")
    semantic_rows = [
        semantic_result(**row) if isinstance(row, Mapping) and row.get("schema_version") is None else dict(row)
        for row in semantic
    ]
    agentic_rows = [
        agentic_result(**row) if isinstance(row, Mapping) and row.get("schema_version") is None else dict(row)
        for row in agentic
    ]
    for row in semantic_rows:
        _require(row.get("provenance"), "missing-provenance", "semantic result has no provenance")
    for row in agentic_rows:
        _require(row.get("provenance"), "missing-provenance", "agentic result has no provenance")
    declared = classification or max(
        [str(row.get("classification") or "internal") for row in [*semantic_rows, *agentic_rows]] or ["internal"],
        key=lambda name: _RANK.get(name, -1),
    )
    _require(_RANK.get(declared, -1) >= 0, "unknown-classification", "bundle classification is unknown")
    highest = max(
        [str(row.get("classification") or "internal") for row in [*semantic_rows, *agentic_rows]] or ["internal"],
        key=lambda name: _RANK.get(name, -1),
    )
    _require(_RANK[declared] >= _RANK[highest], "classification-downgrade", "bundle classification lowers a result")
    moment = generated_at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    semantic_rows.sort(key=lambda row: str(row.get("result_id") or ""))
    agentic_rows.sort(key=lambda row: str(row.get("result_id") or ""))
    return {
        "schema_version": BUNDLE_SCHEMA,
        "query": _nonempty(query, "query"),
        "semantic": semantic_rows,
        "agentic": agentic_rows,
        "generated_at": moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "classification": declared,
        "department": department,
        "vault_id": vault_id,
    }


def render_for_agent(payload: Mapping[str, Any]) -> str:
    """Render both sets with provenance; never a merged answer."""
    _require(payload.get("schema_version") == BUNDLE_SCHEMA, "malformed-bundle", "unknown hybrid bundle")
    lines = [f"# Retrieval bundle: {payload.get('query', '')}", ""]
    for heading, key in (("Semantic results", "semantic"), ("Agentic results", "agentic")):
        lines.append(f"## {heading}")
        rows = payload.get(key) or []
        if not rows:
            lines.append("(none)")
        for row in rows:
            provenance = row.get("provenance") if isinstance(row, Mapping) else {}
            lines.append(
                "- `{}` — source `{}` — provenance `{}`".format(
                    row.get("result_id", ""),
                    (provenance or {}).get("source", ""),
                    json.dumps(provenance, sort_keys=True),
                )
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
