#!/usr/bin/env bash
# PEER-SIDE: run this node's part of the cluster election (the member ceremony).
#
# Files this node's bid (operational key included), votes for --candidate
# (default: this node — a peer requesting to become master votes for itself),
# waits for the unanimous cluster trust root (every member's co-signature,
# 2 of 2 in a pair, 3 of 3 in the full cluster), co-signs it, and installs
# the result on this node's identity.
#
# Nothing here grants anything on its own. The root forms only when every
# member runs their half — the others run this same script with
#   CANDIDATE=<requester> make election-request
# on their own nodes. Install and refusal rules: shared/sparknet-election.py,
# normative in contracts/consensus-protocol.md.
#
# A third member joins with the same member ceremony plus the admission flags:
#   --admission --members <roster> --trust <parent-root> --as-member --root-out <new-root>
# An existing peer adopts the child by passing its held root as --root-doc and
# --as-member. The Master keeps --root-doc and omits --as-member. Every join
# participant passes the out-of-band roster so the exchange cannot substitute
# a different new member.
set -euo pipefail

ROLE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=./sparknet-env.sh
. "$ROLE_ROOT/scripts/sparknet-env.sh"

URL="${SPARKNET_ELECTION_URL:-http://127.0.0.1:4210}"
NODE_ID=""
CANDIDATE=""
IDENTITY="$ROLE_ROOT/manifests/node-identity.json"
KEY_DIR="${SPARKNET_KEY_DIR:-$SPARKNET_STATE_DIR/keys}"
WAIT="${SPARKNET_ELECTION_WAIT:-30}"
ROOT_DOC=""
ROOT_OUT=""
TRUST=""
MEMBERS=""
AS_MEMBER=0
ADMISSION=0

usage() {
  cat <<USAGE
usage: sparknet-election-request.sh [--url <url>] [--candidate <node-id>]
                                    [--node-id <node-id>] [--identity <file>]
                                    [--key-dir <dir>] [--wait-seconds <n>]
                                    [--root-doc <file>] [--root-out <file>]
                                    [--trust <parent-root>] [--members <roster>]
                                    [--admission] [--as-member]

Runs the full member ceremony against a running election exchange:
bid -> vote -> wait for the root -> co-sign -> install on --identity.

Admission (join) uses the same ceremony:
  new member     --admission --members <roster> --trust <parent-root> --as-member --root-out <new-root>
  existing peer  --admission --members <roster> --root-doc <held-root> --as-member
  elected Master --admission --members <roster> --root-doc <held-root>

Defaults:
  --url          \$SPARKNET_ELECTION_URL or http://127.0.0.1:4210
  --candidate    this node's id (self-request)
  --node-id      read from --identity
  --identity     $ROLE_ROOT/manifests/node-identity.json
  --key-dir      \$SPARKNET_KEY_DIR or \$SPARKNET_STATE_DIR/keys
  --wait-seconds \$SPARKNET_ELECTION_WAIT or 30
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="$2"; shift 2 ;;
    --node-id) NODE_ID="$2"; shift 2 ;;
    --candidate) CANDIDATE="$2"; shift 2 ;;
    --identity) IDENTITY="$2"; shift 2 ;;
    --key-dir) KEY_DIR="$2"; shift 2 ;;
    --wait-seconds) WAIT="$2"; shift 2 ;;
    --root-doc) ROOT_DOC="$2"; shift 2 ;;
    --root-out) ROOT_OUT="$2"; shift 2 ;;
    --trust) TRUST="$2"; shift 2 ;;
    --members) MEMBERS="$2"; shift 2 ;;
    --as-member) AS_MEMBER=1; shift ;;
    --admission) ADMISSION=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

[ -f "$IDENTITY" ] || { echo "identity not found: $IDENTITY" >&2; exit 1; }

if [ "$AS_MEMBER" = 1 ] && [ -z "$MEMBERS" ]; then
  echo "--as-member requires --members with the out-of-band roster" >&2
  exit 1
fi
if [ "$ADMISSION" = 1 ] && [ -z "$MEMBERS" ]; then
  echo "--admission requires --members with the out-of-band roster" >&2
  exit 1
fi

PY="${PY:-$SPARKNET_FABRIC_ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
ELECTION="$SPARKNET_SHARED_DIR/sparknet-election.py"
[ -f "$ELECTION" ] || { echo "election implementation not found: $ELECTION" >&2; exit 1; }

if [ -z "$NODE_ID" ]; then
  NODE_ID="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("node_id",""))' "$IDENTITY")"
fi
case "$NODE_ID" in
  ""|REPLACE_WITH_NODE_ID)
    echo "identity $IDENTITY has no real node_id yet (run: make user-init)" >&2
    exit 1
    ;;
esac
CANDIDATE="${CANDIDATE:-$NODE_ID}"

# The cluster key signs the bid and the vote; without it this node cannot be
# a member. Its public half must also be what membership lists — the
# exchange checks that, and refuses a bid signed by anything else.
if [ ! -f "$KEY_DIR/cluster.ed25519" ]; then
  echo "no cluster key at $KEY_DIR/cluster.ed25519 — create one first:" >&2
  echo "  $PY $ELECTION keygen --key-dir $KEY_DIR" >&2
  exit 1
fi

ARGS=(
  --url "$URL"
  --node-id "$NODE_ID"
  --candidate "$CANDIDATE"
  --identity "$IDENTITY"
  --key-dir "$KEY_DIR"
  --wait-seconds "$WAIT"
)
if [ -n "$ROOT_DOC" ]; then ARGS+=(--root-doc "$ROOT_DOC"); fi
if [ -n "$ROOT_OUT" ]; then ARGS+=(--root-out "$ROOT_OUT"); fi
if [ -n "$TRUST" ]; then ARGS+=(--trust "$TRUST"); fi
if [ -n "$MEMBERS" ]; then ARGS+=(--members "$MEMBERS"); fi
if [ "$AS_MEMBER" = 1 ]; then ARGS+=(--as-member); fi
if [ "$ADMISSION" = 1 ]; then ARGS+=(--admission); fi

exec "$PY" "$ELECTION" --shared-dir "$SPARKNET_SHARED_DIR" member "${ARGS[@]}"
