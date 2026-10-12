"""브리지 연결 — 코드로 맺고, 토큰은 해시만 남기고, 끊으면 바로 닿지 않는다."""

import http.client
import json

import pytest

from app.bridge import pairing, service as bridge_service
from app.bridge.policy import DEFAULT_PORT
from app.bridge.service import BridgeService
from app.bridge.store import AUDIT_NAME, BridgeStore

EXTENSION = "a" * 32
OTHER = "b" * 32
ORIGIN = f"chrome-extension://{EXTENSION}"


def call(port, method, path, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {**(headers or {}), **({"Content-Type": "application/json"} if data else {})}
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"null")
    finally:
        connection.close()


@pytest.fixture
def service(tmp_path):
    store = BridgeStore(tmp_path / "appdata" / "bridge.json")
    store.port = 0                      # 시험은 운영체제가 고른 포트를 쓴다
    service = BridgeService(store)
    service.describe = lambda: {"project": "시험 인수인계"}
    yield service
    service.stop()


def pair(service, code, origin=ORIGIN, label="onNara.sAIde"):
    return call(service.port, "POST", "/bridge/v1/pair", {"Origin": origin} if origin else None,
                {"code": code, "label": label})


# ── 코드와 토큰 ─────────────────────────────────────────────────────
def test_code_is_six_digits_lives_five_minutes_and_dies_after_five_misses():
    code = pairing.create_code(now=1000)
    assert len(code.code) == 6 and code.code.isdigit() and code.expires_at == 1300
    assert pairing.check_code(None, code.code, now=1000) == "missing"
    assert pairing.check_code(code, f" {code.code[:3]} {code.code[3:]} ", now=1299) == "ok"    # 띄어 적어도 받는다
    assert pairing.check_code(code, code.code, now=1301) == "expired"
    wrong = "000000" if code.code != "000000" else "111111"
    fresh = pairing.create_code(now=1000)
    fresh.code = code.code
    assert [pairing.check_code(fresh, wrong, now=1001) for _ in range(5)] == ["mismatch"] * 4 + ["exhausted"]
    assert pairing.check_code(fresh, fresh.code, now=1002) == "exhausted"                       # 맞는 코드도 이제 늦었다
    assert pairing.check_code(pairing.create_code(now=1000), None, now=1001) == "mismatch"


def test_tokens_are_random_and_compared_by_hash():
    first, second = pairing.generate_token(), pairing.generate_token()
    assert first != second and len(first) >= 43
    assert pairing.digests_equal(pairing.hash_token(first), pairing.hash_token(first))
    assert not pairing.digests_equal(pairing.hash_token(first), pairing.hash_token(second))
    assert not pairing.digests_equal("", "")


# ── 저장 ────────────────────────────────────────────────────────────
def test_store_starts_closed_and_never_keeps_the_token_itself(tmp_path):
    path = tmp_path / "appdata" / "bridge.json"
    store = BridgeStore(path)
    assert (store.enabled, store.port, store.clients()) == (False, DEFAULT_PORT, [])
    token = pairing.generate_token()
    store.set_enabled(True)
    store.set_port(50123)
    client = store.add_client(EXTENSION, pairing.hash_token(token), ("read",), label="  내   확장 ")
    assert client["label"] == "내 확장" and "token_hash" not in client
    text = path.read_text(encoding="utf-8")
    assert token not in text and pairing.hash_token(token) in text
    again = BridgeStore(path)
    assert (again.enabled, again.port) == (True, 50123) and again.clients() == [client]
    assert again.find(pairing.hash_token(token), pairing.digests_equal)["id"] == client["id"]
    with pytest.raises(ValueError):
        store.set_port(80)


def test_same_extension_pairing_again_kills_the_old_token(tmp_path):
    store = BridgeStore(tmp_path / "bridge.json")
    old, new = pairing.hash_token("o" * 43), pairing.hash_token("n" * 43)
    store.add_client(EXTENSION, old, ("read",))
    store.add_client(OTHER, pairing.hash_token("x" * 43), ("read",))
    store.add_client(EXTENSION, new, ("read",))
    assert sorted(c["extension_id"] for c in store.clients()) == [EXTENSION, OTHER]
    assert store.find(old, pairing.digests_equal) is None and store.find(new, pairing.digests_equal)


def test_damaged_file_means_closed_with_no_clients(tmp_path):
    path = tmp_path / "bridge.json"
    path.write_text("{not json", encoding="utf-8")
    assert (BridgeStore(path).enabled, BridgeStore(path).clients()) == (False, [])
    path.write_text(json.dumps({"enabled": "yes", "port": 80, "clients": [
        {"id": "x", "extension_id": EXTENSION, "token_hash": "short", "scopes": ["read"]},
        {"id": "y", "extension_id": "not-an-id", "token_hash": "0" * 64},
        {"id": "z", "extension_id": EXTENSION, "token_hash": "0" * 64, "scopes": ["read", "admin"]}]}), encoding="utf-8")
    store = BridgeStore(path)
    assert (store.enabled, store.port) == (False, DEFAULT_PORT)                                  # 켜짐은 true일 때만
    assert [(c["id"], c["scopes"]) for c in store.clients()] == [("z", ["read"])]               # 모르는 권한은 버린다


# ── 켜고 끄기와 연결 ────────────────────────────────────────────────
def test_port_stays_closed_until_the_user_turns_it_on(service):
    assert service.start_if_enabled() is False and not service.running
    with pytest.raises(ValueError):
        service.new_code()
    assert service.set_enabled(True) and service.running and service.store.enabled
    assert call(service.port, "GET", "/bridge/v1/status")[1]["paired"] is False
    port = service.port
    assert service.set_enabled(False) is False and not service.running
    with pytest.raises(OSError):
        call(port, "GET", "/bridge/v1/status")


def test_taken_port_is_reported_not_worked_around(service, tmp_path):
    service.set_enabled(True)
    other = BridgeService(BridgeStore(tmp_path / "other.json"))
    other.store.port = service.port
    try:
        assert other.set_enabled(True) is False and not other.running
        assert str(service.port) in other.error and other.store.enabled                           # 설정은 켜짐, 상태는 열지 못함
    finally:
        other.stop()


def test_pairing_by_code_gives_one_token_bound_to_that_extension(service):
    service.set_enabled(True)
    assert pair(service, "123456") == (409, {"code": "pairing_not_started", "message": "눈치코치 설정에서 연결 코드를 먼저 만드세요."})
    code = service.new_code().code
    assert pair(service, code, origin=None)[1]["code"] == "origin_required"                       # 확장이 아닌 곳에서는 못 맺는다
    assert pair(service, code, origin="https://evil.test")[1]["code"] == "origin_rejected"
    status, body = pair(service, code)
    assert status == 200 and body["scopes"] == ["read", "write"] and len(body["token"]) >= 43
    assert pair(service, code)[1]["code"] == "pairing_not_started"                                # 코드는 한 번만 쓴다
    assert service.pending_code() is None

    headers = {"Authorization": f"Bearer {body['token']}"}
    status, state = call(service.port, "GET", "/bridge/v1/status", {**headers, "Origin": ORIGIN})
    assert state["paired"] is True and state["project"] == "시험 인수인계" and state["scopes"] == ["read", "write"]
    assert call(service.port, "GET", "/bridge/v1/status", {**headers, "Origin": f"chrome-extension://{OTHER}"})[1]["code"] == "origin_mismatch"

    [client] = service.clients()
    assert client["id"] == body["clientId"] and client["label"] == "onNara.sAIde"
    stored = service.store.path.read_text(encoding="utf-8")
    assert body["token"] not in stored
    audit = service.store.path.with_name(AUDIT_NAME).read_text(encoding="utf-8")
    assert '"result": "ok"' in audit and body["token"] not in audit and code not in audit

    assert service.revoke(client["id"]) and service.clients() == []
    assert call(service.port, "GET", "/bridge/v1/status", headers)[1]["paired"] is False          # 끊으면 바로 낯선 이다


def test_wrong_codes_use_the_code_up(service):
    service.set_enabled(True)
    code = service.new_code().code
    wrong = "000000" if code != "000000" else "111111"
    codes = [pair(service, wrong)[1]["code"] for _ in range(5)]
    assert codes == ["pairing_code_mismatch"] * 4 + ["pairing_code_exhausted"]
    assert pair(service, code)[1]["code"] == "pairing_not_started"
    assert service.clients() == []


def test_connection_survives_a_restart_of_the_app(service, tmp_path):
    service.set_enabled(True)
    token = pair(service, service.new_code().code)[1]["token"]
    service.stop()
    reopened = BridgeService(BridgeStore(service.store.path))
    reopened.store.port = 0
    try:
        assert reopened.start_if_enabled()                                                       # 켜 둔 설정이 남아 있다
        assert call(reopened.port, "GET", "/bridge/v1/status", {"Authorization": f"Bearer {token}"})[1]["paired"] is True
    finally:
        reopened.stop()


# ── 설정 화면 ───────────────────────────────────────────────────────
def test_settings_card_turns_the_bridge_on_shows_the_code_and_lists_connections(service, tmp_path):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton
    from app.db import Database
    from app.ui.views.settings import SettingsDialog
    qapp = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "project" / "project.db")
    db.init()
    plain = SettingsDialog(db)
    assert not hasattr(plain, "bridge_enabled")                       # 앱이 브리지를 띄우지 않은 자리(시험·도구)에는 카드가 없다
    plain.close()
    bridge_service.install(service)
    dialog = SettingsDialog(db)
    try:
        assert not dialog.bridge_enabled.isChecked() and "포트를 열지 않습니다" in dialog.bridge_state.text()
        assert not dialog.bridge_code_button.isEnabled()
        dialog.bridge_enabled.setChecked(True)
        assert service.running and "켜져 있습니다" in dialog.bridge_state.text() and dialog.bridge_code_button.isEnabled()
        dialog.bridge_code_button.click()
        code = service.pending_code().code
        assert f"{code[:3]} {code[3:]}" in dialog.bridge_code.text()

        assert pair(service, code)[0] == 200                          # 확장이 코드를 입력했다(서버 스레드에서 일어난다)
        dialog._sync_bridge()
        assert dialog.bridge_code.text() == "" and "연결된 확장 1개" in dialog.bridge_state.text()
        assert any("onNara.sAIde · 확장 ID aaaaaa…" in label.text() for label in dialog.findChildren(QLabel))
        next(button for button in dialog.findChildren(QPushButton) if button.text() == "연결 해제").click()
        assert service.clients() == []
        dialog._sync_bridge()
        assert "연결된 확장 0개" in dialog.bridge_state.text()

        dialog.bridge_enabled.setChecked(False)
        assert not service.running and "꺼져 있습니다" in dialog.bridge_state.text()
        actions = [row["action"] for row in db.con.execute("SELECT action FROM audit_logs ORDER BY id")]
        assert [a for a in actions if a.startswith("bridge.")] == ["bridge.enable", "bridge.revoke", "bridge.disable"]
    finally:
        bridge_service.install(None)
        dialog._bridge_timer.stop()
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()
        db.close()
