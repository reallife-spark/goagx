"""Node auth gateway: header policy, override precedence and the HTTP contract."""

from __future__ import annotations

import base64
import http.client
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sparknet_loader import load_by_path

ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "peer-node" / "services" / "sparknet-auth-gateway.py"
gateway = load_by_path("sparknet_auth_gateway_test", SERVICE)
authz = load_by_path("authz_auth_gateway_test", ROOT / "shared" / "sparknet-authz.py")
sso = load_by_path("sso_auth_gateway_test", ROOT / "shared" / "sparknet-sso.py")
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
FINGERPRINT = "140e0dc554e18b883922bd5164f576eee0c1d6d08ebf5f7d3c052948257c36c7"


def _policy(tmp_path: Path, overrides: dict | None = None):
    state = tmp_path / "state"
    state.mkdir()
    if overrides is not None:
        (state / "admin-role-overrides.json").write_text(json.dumps(overrides), encoding="utf-8")
    return gateway.AuthPolicy(state)


def _oidc(email: str, groups: str):
    return {"X-Auth-Request-Email": email, "X-Auth-Request-Groups": groups}


def test_oidc_admin_and_user_roles(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    admin = policy.decide(_oidc("admin@example.test", "sparknet-admins"), source_is_loopback=True)
    assert admin["X-User-Email"] == "admin@example.test"
    assert admin["X-User-Role"] == "admin"
    assert admin["X-Auth-Source"] == "oidc"
    user = policy.decide(_oidc("user@example.test", "sparknet-users"), source_is_loopback=True)
    assert user["X-User-Role"] == "user"


def test_unapproved_and_missing_identity_are_refused(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    with pytest.raises(gateway.Decision) as unapproved:
        policy.decide(_oidc("x@example.test", "other"), source_is_loopback=True)
    assert (unapproved.value.status, unapproved.value.reason) == (403, "unapproved")
    with pytest.raises(gateway.Decision) as missing:
        policy.decide({}, source_is_loopback=True)
    assert (missing.value.status, missing.value.reason) == (401, "no-identity")


def test_non_loopback_caller_cannot_claim_an_identity(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    with pytest.raises(gateway.Decision) as refusal:
        policy.decide(_oidc("admin@example.test", "sparknet-admins"), source_is_loopback=False)
    assert (refusal.value.status, refusal.value.reason) == (403, "not-loopback")


def test_mtls_admin_is_the_offline_path(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    headers = {
        "X-Client-Verify": "SUCCESS",
        "X-Client-DN": "CN=sparknet-admin,emailAddress=Admin@Example.Test",
    }
    decided = policy.decide(headers, source_is_loopback=True)
    assert decided["X-User-Email"] == "admin@example.test"
    assert decided["X-User-Role"] == "admin"
    assert decided["X-Auth-Source"] == "mtls-admin"


def test_mtls_without_an_email_identity_is_refused(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    with pytest.raises(gateway.Decision) as refusal:
        policy.decide(
            {"X-Client-Verify": "SUCCESS", "X-Client-DN": "CN=sparknet-admin"},
            source_is_loopback=True,
        )
    assert (refusal.value.status, refusal.value.reason) == (401, "bad-client-cert")


def test_override_wins_and_conflict_is_recorded(tmp_path: Path) -> None:
    policy = _policy(
        tmp_path,
        {
            "admin@example.test": {
                "role": "user",
                "set_by": "master-1",
                "request_id": "admin-1",
                "set_at": "2026-11-16T12:00:00Z",
            }
        },
    )
    decided = policy.decide(_oidc("admin@example.test", "sparknet-admins"), source_is_loopback=True)
    assert decided["X-User-Role"] == "user"
    events = (tmp_path / "state" / "admin-events" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(events) == 1
    record = json.loads(events[0])
    assert record["kind"] == "role-override-conflict"
    assert record["idp_role"] == "admin"
    assert record["override_role"] == "user"


def test_override_can_promote_a_user(tmp_path: Path) -> None:
    policy = _policy(
        tmp_path,
        {
            "user@example.test": {
                "role": "admin",
                "set_by": "master-1",
                "request_id": "admin-2",
                "set_at": "2026-11-16T12:00:00Z",
            }
        },
    )
    decided = policy.decide(_oidc("user@example.test", "sparknet-users"), source_is_loopback=True)
    assert decided["X-User-Role"] == "admin"


def test_http_verify_returns_the_injected_headers(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    server = gateway.build_server(policy, "127.0.0.1", 0)
    server.start()
    try:
        port = server.servers[0].server_address[1]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "GET",
            "/auth/verify",
            headers={
                "X-Auth-Request-Email": "admin@example.test",
                "X-Auth-Request-Groups": "sparknet-admins",
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("X-User-Email") == "admin@example.test"
        assert response.getheader("X-User-Role") == "admin"
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/auth/verify")
        refused = connection.getresponse()
        assert refused.status == 401
        assert json.loads(refused.read())["reason"] == "no-identity"
        connection.close()
    finally:
        server.stop()


def test_health_endpoint_does_not_decide_an_identity(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    server = gateway.build_server(policy, "127.0.0.1", 0)
    server.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.servers[0].server_address[1], timeout=5)
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["ok"] is True
        connection.close()
    finally:
        server.stop()


class _FakeOIDC(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003 - base signature
        return

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.headers.get("Cookie") != "session=ok":
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(202)
        self.send_header("X-Auth-Request-Email", "admin@example.test")
        self.send_header("X-Auth-Request-Groups", "sparknet-admins")
        self.end_headers()


def test_oidc_session_is_verified_through_the_local_proxy(tmp_path: Path) -> None:
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOIDC)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        policy = gateway.AuthPolicy(
            tmp_path / "state",
            oidc_auth_url=f"http://127.0.0.1:{proxy.server_port}/oauth2/auth",
        )
        decided = policy.decide({}, source_is_loopback=True, cookie="session=ok")
        assert decided["X-User-Email"] == "admin@example.test"
        assert decided["X-User-Role"] == "admin"
        with pytest.raises(gateway.Decision) as refusal:
            policy.decide({}, source_is_loopback=True, cookie="session=bad")
        assert (refusal.value.status, refusal.value.reason) == (401, "no-identity")
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=5)


def test_unreachable_oidc_proxy_is_a_refusal_not_a_fallback(tmp_path: Path) -> None:
    policy = gateway.AuthPolicy(
        tmp_path / "state",
        oidc_auth_url="http://127.0.0.1:1/oauth2/auth",
        http_timeout=0.2,
    )
    with pytest.raises(gateway.Decision) as refusal:
        policy.decide({}, source_is_loopback=True, cookie="session=ok")
    assert (refusal.value.status, refusal.value.reason) == (503, "idp-unreachable")


def _master_assertion(key, **overrides):
    record = {
        "schema": authz.SSO_ASSERTION_SCHEMA,
        "assertion_id": "assertion-1",
        "master_node_id": "master-1",
        "target_node_id": "peer-fixture-01",
        "target_fingerprint": FINGERPRINT,
        "subject": "human-1",
        "email": "admin@example.test",
        "role": "admin",
        "groups": ["sparknet-admins"],
        "issued_at": int(NOW.timestamp()) - 5,
        "expires_at": int(NOW.timestamp()) + 60,
        "nonce": "n1",
    }
    record.update(overrides)
    return sso.sign_assertion(record, key)


def _policy_with_master(tmp_path: Path, public_key):
    return gateway.AuthPolicy(
        tmp_path / "state",
        node_id="peer-fixture-01",
        node_fingerprint=FINGERPRINT,
        master_pubkey=public_key,
    )


def test_master_assertion_is_verified_and_replay_is_refused(tmp_path: Path) -> None:
    private_b64, _public = authz.generate_keypair()
    key = authz.private_key_from_b64(private_b64)
    policy = _policy_with_master(tmp_path, key.public_key())
    assertion = _master_assertion(key)
    header = base64.b64encode(json.dumps(assertion).encode()).decode()
    decided = policy.decide({"X-Sparknet-Assertion": header}, source_is_loopback=True, now=NOW)
    assert decided["X-User-Email"] == "admin@example.test"
    assert decided["X-User-Role"] == "admin"
    assert decided["X-Auth-Source"] == "master-assertion"
    with pytest.raises(gateway.Decision) as replay:
        policy.decide({"X-Sparknet-Assertion": header}, source_is_loopback=True, now=NOW)
    assert (replay.value.status, replay.value.reason) == (401, "assertion-replayed")


def test_master_assertion_for_another_node_is_refused(tmp_path: Path) -> None:
    private_b64, _public = authz.generate_keypair()
    key = authz.private_key_from_b64(private_b64)
    policy = _policy_with_master(tmp_path, key.public_key())
    assertion = _master_assertion(key, target_node_id="peer-other")
    header = base64.b64encode(json.dumps(assertion).encode()).decode()
    with pytest.raises(gateway.Decision) as refusal:
        policy.decide({"X-Sparknet-Assertion": header}, source_is_loopback=True, now=NOW)
    assert (refusal.value.status, refusal.value.reason) == (403, "assertion-wrong-node")


def test_assertion_without_a_master_key_is_refused(tmp_path: Path) -> None:
    policy = gateway.AuthPolicy(tmp_path / "state")
    with pytest.raises(gateway.Decision) as refusal:
        policy.decide({"X-Sparknet-Assertion": "e30="}, source_is_loopback=True, now=NOW)
    assert (refusal.value.status, refusal.value.reason) == (503, "no-master-key")


def test_trust_root_supplies_the_master_key_and_node_fingerprint(tmp_path: Path) -> None:
    _private_b64, public_b64 = authz.generate_keypair()
    trust_root = tmp_path / "trust-root.json"
    trust_root.write_text(
        json.dumps(
            {
                "master_pubkey": public_b64,
                "members": [{"node_id": "peer-fixture-01", "fingerprint": FINGERPRINT}],
            }
        ),
        encoding="utf-8",
    )
    public_key, fingerprint = gateway._trust_root_binding(trust_root, "peer-fixture-01")
    assert public_key is not None
    assert fingerprint == FINGERPRINT
    assert gateway._trust_root_binding(None, "peer-fixture-01") == (None, "")


def test_admin_ingress_refuses_a_user_role(tmp_path: Path) -> None:
    policy = gateway.AuthPolicy(tmp_path / "state", require_admin=True)
    with pytest.raises(gateway.Decision) as refusal:
        policy.decide(_oidc("user@example.test", "sparknet-users"), source_is_loopback=True)
    assert (refusal.value.status, refusal.value.reason) == (403, "admin-required")
    assert (
        policy.decide(_oidc("admin@example.test", "sparknet-admins"), source_is_loopback=True)["X-User-Role"] == "admin"
    )
