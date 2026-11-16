#!/usr/bin/env python3
"""Node auth gateway — the one identity injector in front of Open WebUI.

The gateway is a loopback-only decision point. It does not perform OIDC itself
and it does not proxy chat traffic; nginx calls it as an ``auth_request``
subrequest and copies the three returned headers into the upstream Open WebUI
request. That keeps the security-critical part small and testable:

- trust only loopback (the nginx ingress), never a client-supplied header;
- online: accept the identity an OIDC proxy already verified and map the IdP
  group to a role;
- offline: accept the admin client certificate nginx verified against the
  fabric admin CA;
- apply the per-node signed override, record a conflict, and never silently
  drop either side;
- emit ``X-User-Email`` / ``X-User-Role`` / ``X-User-Groups`` only after the
  decision.

``contracts/sso-identity.md`` is the normative rule. The signed artifacts come
from ``shared/sparknet-sso.py``; this service adds no crypto of its own.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SERVICES_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVICES_DIR))
sys.path.insert(0, str(SERVICES_DIR.parent.parent / "shared"))

try:
    from sparknet_loader import load_shared
except ImportError:  # pragma: no cover - the bundle always carries the loader
    raise SystemExit("cannot load shared/sparknet_loader.py; the gateway carries no policy copy")

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8190
EVENTS_DIRNAME = "admin-events"
EVENTS_FILENAME = "events.jsonl"

EMAIL_IN_DN = re.compile(r"(?:emailAddress|email)=([^,]+)", re.IGNORECASE)
CN_IN_DN = re.compile(r"CN=([^,]+)", re.IGNORECASE)


def _load_sso() -> Any:
    return load_shared(
        "sparknet-sso.py",
        "sparknet_sso_gateway",
        required=("sparknet-authz.py", "sparknet-sso.py"),
        error=RuntimeError,
        message="cannot find shared/sparknet-sso.py; the gateway carries no policy copy",
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Decision(Exception):
    """A refusal with an HTTP status and a stable reason code."""

    def __init__(self, status: int, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.detail = detail


class AuthPolicy:
    """The identity decision, independent of the HTTP wrapper."""

    def __init__(
        self,
        state_dir: str | Path,
        *,
        trust_loopback_only: bool = True,
        oidc_auth_url: str | None = None,
        http_timeout: float = 5.0,
        node_id: str = "",
        node_fingerprint: str = "",
        master_pubkey: Any = None,
        require_admin: bool = False,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.trust_loopback_only = trust_loopback_only
        self.oidc_auth_url = (oidc_auth_url or "").strip() or None
        self.http_timeout = http_timeout
        self.node_id = node_id
        self.node_fingerprint = node_fingerprint
        self.master_pubkey = master_pubkey
        self.require_admin = require_admin
        self.seen_nonces: set[str] = set()
        self.sso = _load_sso()

    @property
    def override_path(self) -> Path:
        return self.state_dir / self.sso.OVERRIDE_FILENAME

    def overrides(self) -> dict[str, dict[str, Any]]:
        try:
            return self.sso.load_overrides(self.override_path)
        except self.sso.SSOError as exc:
            raise Decision(500, "override-file-invalid", str(exc)) from exc

    def decide(
        self,
        headers: Mapping[str, str],
        *,
        source_is_loopback: bool,
        cookie: str = "",
        now: datetime | None = None,
    ) -> dict[str, str]:
        """Return the headers to inject, or raise ``Decision``.

        Only loopback may ask. A caller that reaches the gateway directly is not
        the nginx ingress, so it has no standing to claim an identity.
        """
        if self.trust_loopback_only and not source_is_loopback:
            raise Decision(403, "not-loopback", "the auth gateway is called only by the local ingress")

        instant = now or datetime.now(timezone.utc)
        assertion = str(headers.get("X-Sparknet-Assertion") or "").strip()
        if assertion:
            payload = self._assertion_identity(assertion, instant)
            email = str(payload["email"]).lower()
            groups = list(payload["groups"])
            idp_role = str(payload["role"])
            source = "master-assertion"
        else:
            client_verify = str(headers.get("X-Client-Verify") or "").strip().upper()
            if client_verify == "SUCCESS":
                email = self._email_from_dn(headers.get("X-Client-DN", ""))
                if not email:
                    raise Decision(401, "bad-client-cert", "the client certificate carries no email identity")
                groups = [self.sso.ADMIN_GROUP]
                idp_role = "admin"
                source = "mtls-admin"
            else:
                email = str(headers.get("X-Auth-Request-Email") or "").strip().lower()
                groups = self._groups(headers.get("X-Auth-Request-Groups", ""))
                if not email:
                    identity = self._verify_oidc_session(cookie)
                    if identity is None:
                        raise Decision(
                            401,
                            "no-identity",
                            "neither an OIDC session nor an admin certificate was presented",
                        )
                    email, groups = identity
                if "@" not in email:
                    raise Decision(401, "no-identity", "the OIDC identity is not an email address")
                idp_role = self.sso.role_for_groups(groups)
                if idp_role is None:
                    raise Decision(403, "unapproved", "the identity is in neither allowed IdP group")
                source = "oidc"

        override = self.overrides().get(email)
        role, conflict = self.sso.effective_role(idp_role, override)
        if conflict:
            self._record_conflict(email, idp_role, role, override)
        if role not in self.sso.ROLES:
            raise Decision(403, "unapproved", "the effective role is not an allowed role")
        if self.require_admin and role != "admin":
            raise Decision(403, "admin-required", "this ingress accepts the admin role only")
        injected = self.sso.injected_headers(email, role, groups)
        injected["X-Auth-Source"] = source
        return injected

    def _assertion_identity(self, raw: str, now: datetime) -> dict[str, Any]:
        """Verify the Master proxy's short-lived assertion for this node."""
        if self.master_pubkey is None or not self.node_id or not self.node_fingerprint:
            raise Decision(503, "no-master-key", "the gateway has no Master public key or node binding")
        try:
            payload = json.loads(base64.b64decode(raw.encode("ascii"), validate=True))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise Decision(401, "malformed-assertion", "the assertion is not base64 JSON") from exc
        ok, reason = self.sso.verify_assertion(
            payload,
            self.master_pubkey,
            expected_node_id=self.node_id,
            expected_fingerprint=self.node_fingerprint,
            now=now,
            seen_nonces=self.seen_nonces,
        )
        if not ok:
            status = 403 if reason in ("assertion-wrong-node", "role-group-mismatch") else 401
            raise Decision(status, reason or "bad-assertion", "the Master assertion did not verify")
        self.seen_nonces.add(str(payload["nonce"]))
        return payload

    def _verify_oidc_session(self, cookie: str) -> tuple[str, list[str]] | None:
        """Ask the local OIDC proxy whether this browser session is valid.

        The gateway is the only component nginx calls, so it owns the choice
        between the online and offline paths. It never parses an OIDC token
        itself: oauth2-proxy has already done that, and this call only reads
        the identity headers it returns. A missing or unreachable proxy is a
        refusal, not a fallback to an unauthenticated request.
        """
        if not self.oidc_auth_url or not cookie:
            return None
        request = urllib.request.Request(self.oidc_auth_url, headers={"Cookie": cookie, "Accept": "text/plain"})
        try:
            with urllib.request.urlopen(request, timeout=self.http_timeout) as response:
                status = getattr(response, "status", 200)
                if status not in (200, 202):
                    return None
                email = str(response.headers.get("X-Auth-Request-Email") or "").strip().lower()
                groups = self._groups(response.headers.get("X-Auth-Request-Groups", ""))
        except urllib.error.HTTPError:
            return None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise Decision(503, "idp-unreachable", f"the OIDC proxy did not answer: {exc}") from exc
        if not email or "@" not in email:
            return None
        return email, groups

    def _email_from_dn(self, dn: Any) -> str:
        text = str(dn or "")
        match = EMAIL_IN_DN.search(text) or CN_IN_DN.search(text)
        if not match:
            return ""
        candidate = match.group(1).strip().strip('"')
        return candidate.lower() if "@" in candidate else ""

    def _groups(self, raw: Any) -> list[str]:
        if isinstance(raw, (list, tuple)):
            return [str(item).strip() for item in raw if str(item).strip()]
        return [part.strip() for part in str(raw or "").split(",") if part.strip()]

    def _record_conflict(
        self,
        email: str,
        idp_role: str | None,
        override_role: str,
        override: Mapping[str, Any] | None,
    ) -> None:
        events_dir = self.state_dir / EVENTS_DIRNAME
        events_dir.mkdir(parents=True, exist_ok=True)
        path = events_dir / EVENTS_FILENAME
        record = {
            "kind": "role-override-conflict",
            "at": _now_iso(),
            "user": email,
            "idp_role": idp_role,
            "override_role": override_role,
            "set_by": (override or {}).get("set_by"),
            "request_id": (override or {}).get("request_id"),
            "detail": "per-node override and IdP group disagree; held for a human",
        }
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)


def _load_http() -> Any:
    return load_shared(
        "sparknet-http.py",
        "sparknet_http_auth_gateway",
        required=("sparknet-authz.py", "sparknet-http.py"),
        error=RuntimeError,
        message="cannot find shared/sparknet-http.py; the gateway carries no second listener",
    )


def build_server(policy: AuthPolicy, bind: str = DEFAULT_BIND, port: int = DEFAULT_PORT) -> Any:
    """Build the shared-transport listener; the route policy is the gateway."""
    http = _load_http()

    def verify(request: Any) -> Any:
        try:
            injected = policy.decide(
                request.headers,
                source_is_loopback=request.source == "loopback",
                cookie=str(request.headers.get("Cookie") or ""),
            )
        except Decision as refusal:
            return http.Response(
                status=refusal.status,
                payload={"ok": False, "reason": refusal.reason, "detail": refusal.detail},
            )
        return http.Response(status=200, raw=b"", headers=dict(injected), content_type="text/plain")

    def health(_request: Any) -> Any:
        return http.Response(status=200, payload={"ok": True, "service": "node-auth-gateway"})

    return http.listen(
        [
            http.Route("/auth/verify", verify, method="GET", auth="plain", source="loopback"),
            http.Route("/healthz", health, method="GET", auth="plain", source="loopback"),
        ],
        name="node-auth-gateway",
        bind=bind,
        port=port,
        health=False,
    )


def _state_dir(vault_root: Path | None) -> Path:
    env = os.environ.get("SPARKNET_STATE_DIR", "").strip()
    if env:
        return Path(env)
    return (vault_root or Path.cwd()) / ".state"


def _load_public_key(path: Path | None) -> Any:
    if path is None:
        return None
    return _load_sso().load_public_key(path)


def _trust_root_binding(path: Path | None, node_id: str) -> tuple[Any, str]:
    """Return ``(master_public_key, node_fingerprint)`` from a trust root."""
    if path is None or not path.is_file():
        return None, ""
    sso = _load_sso()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, ""
    master = data.get("master_pubkey")
    public = sso.public_key_from_b64(master) if isinstance(master, str) and master else None
    fingerprint = ""
    for member in data.get("members") or []:
        if isinstance(member, dict) and member.get("node_id") == node_id:
            fingerprint = str(member.get("fingerprint") or "")
            break
    return public, fingerprint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sparknet node auth gateway (loopback auth_request backend)")
    parser.add_argument("--bind", default=DEFAULT_BIND, help="loopback address; never a wildcard")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--vault-root", type=Path, default=None)
    parser.add_argument(
        "--oidc-auth-url",
        default=os.environ.get("SPARKNET_OIDC_AUTH_URL", "http://127.0.0.1:4180/oauth2/auth"),
        help="local oauth2-proxy /oauth2/auth endpoint; empty disables the online fallback",
    )
    parser.add_argument("--node-id", default=os.environ.get("SPARKNET_NODE_ID", ""))
    parser.add_argument("--node-fingerprint", default=os.environ.get("SPARKNET_NODE_FINGERPRINT", ""))
    parser.add_argument(
        "--master-pubkey",
        type=Path,
        default=None,
        help="base64 Ed25519 public key file for the Master proxy assertion",
    )
    parser.add_argument(
        "--trust-root",
        type=Path,
        default=None,
        help="trust-root.json; supplies the Master public key and this node's fingerprint",
    )
    parser.add_argument("--require-admin", action="store_true", help="refuse any role other than admin on this ingress")
    parser.add_argument("--check", action="store_true", help="validate the override file and exit")
    args = parser.parse_args(argv)
    if args.bind not in ("127.0.0.1", "::1", "localhost") and not args.bind.startswith("127."):
        parser.error("refusing a non-loopback bind: the gateway is called only by the local ingress")
    master_pubkey = _load_public_key(args.master_pubkey)
    node_fingerprint = args.node_fingerprint
    if args.trust_root is not None:
        trust_master, trust_fingerprint = _trust_root_binding(args.trust_root, args.node_id)
        master_pubkey = master_pubkey or trust_master
        node_fingerprint = node_fingerprint or trust_fingerprint
    policy = AuthPolicy(
        args.state_dir or _state_dir(args.vault_root),
        oidc_auth_url=args.oidc_auth_url,
        node_id=args.node_id,
        node_fingerprint=node_fingerprint,
        master_pubkey=master_pubkey,
        require_admin=args.require_admin,
    )
    if args.check:
        try:
            overrides = policy.overrides()
        except Decision as exc:
            print(f"[auth-gateway] {exc.reason}: {exc.detail}", file=sys.stderr)
            return 1
        print(f"[auth-gateway] {len(overrides)} override(s) valid at {policy.override_path}")
        return 0
    server = build_server(policy, args.bind, args.port)
    server.start()
    print(f"[auth-gateway] listening on {args.bind}:{args.port}", file=sys.stderr)
    try:
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
