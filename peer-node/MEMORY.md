# sparknet peer-node — Session Memory

> Standing state only. Read at session start; **replace, never append**.
> The plan and its cursor live in `PLAN.md` — this file points at it.
> History belongs in `02_Daily/YYYY-MM-DD.md` and `00_System/log.md`.

Updated: 2026-10-31

## Plan cursor

- `PLAN.md` — read its `current:` field. Do not mirror step history here.

## Durable facts

- **Current deployment**: `agx64` is the peer (`sparknet-node-0001`,
  `wg1 10.100.0.2/32`) and the fabric's local inference path. `agx32` is the
  elected Master (`sparknet-node-0002`, `10.100.0.1`). `agx32`'s text vLLM is
  deliberately stopped; `agx64` answers over the overlay.
- The peer is the **user-facing node**: local chat, agentic plan/build, vault,
  per-user `peer-<id>` vaults/KBs and self-healing. The Master supplies
  authority and scoped knowledge, not control.
- **Knowledge flow**: local vault/KB first; a missing fabric fact goes through
  the RFI request path; the answer arrives in `01_Inbox/` and is accepted before
  it is knowledge. `../contracts/knowledge-flow.md` is the map.
- **Authority**: no Master operational key, no signer socket, no grant/revoke.
  Protected writes still verify a Master signature under the current registry.
- The shared implementations are read by path: `../contracts/`,
  `../shared/sparknet-authz.py`, `../shared/sparknet-provenance.py`.
- The AI stack is the node's own: Open WebUI on `8181`, own Ollama, own
  ChromaDB. `plan`/`build` are agent sessions; the local model is chat.
- The retired cluster2 pair is **historical proof**, not the current pair. Do
  not use its addresses or its live claims as current state.
- Live nodes run Python 3.8.10 (Ubuntu 20.04): deployed code carries the
  `from __future__ import annotations` / `collections.abc` fallback. Keep newer
  syntax out or gate it the same way.
- The fleet uses iptables (`SPARKNET-FW-*`), not nftables. A published `0.0.0.0`
  port is held private by the firewall, not by the bind.

## Blockers

- Live per-user provisioning, the current-pair admin federation, the 8090 RFI
  request round trip and the RFI responder/timer are `DoD: confirm: user` until
  exercised on `agx64`/`agx32`.
- `4221`/`4223` shared knowledge services are `spec-only`; the RFI loop is the
  v1 knowledge path.

## Local vs live

- This development host has no overlay and no GPU. `make test-all`, `lint`,
  `validate` and `bundle-verify` are provable here; WireGuard, iptables,
  `chattr +i`, systemd, the live signer and the browser surface are not.
- Say "implemented and tested", "deployed live on <host> on <date>", or
  "unverified here" — never generalise a live run.
