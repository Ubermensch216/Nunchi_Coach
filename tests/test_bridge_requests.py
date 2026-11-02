"""브리지 — 눈치코치 → onNara.sAIde 공문 수집 요청 큐 및 온보딩 연동."""

import http.client
import json
import os
import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from test_saide_package import make_package  # noqa: E402

from app.bridge import pairing, service as bridge_service
from app.bridge.service import BridgeService
from app.bridge.store import BridgeStore
from app.db import Database

EXTENSION = "a" * 32
RUN = "20261012-153000-test"


def call(port, method, path, token=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Origin": f"chrome-extension://{EXTENSION}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if data:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"null")
    finally:
        connection.close()


@pytest.fixture
def downloads(tmp_path):
    return tmp_path / "Downloads"


@pytest.fixture
def service(tmp_path, downloads):
    store = BridgeStore(tmp_path / "appdata" / "bridge.json")
    store.port = 0
    store.set_receive_roots([str(downloads)])
    svc = BridgeService(store)
    svc.describe = lambda: {"project": "기획과 인수인계"}
    assert svc.set_enabled(True)
    yield svc
    svc.stop()


@pytest.fixture
def token(service):
    status, body = call(service.port, "POST", "/bridge/v1/pair", body={"code": service.new_code().code, "label": "시험"})
    assert status == 200
    return body["token"]


def test_request_queue_lifecycle_and_progress_reporting(service, token):
    # 인증 없으면 조회 불가
    assert call(service.port, "GET", "/bridge/v1/requests")[0] == 401

    created = service.create_request({
        "listName": "문서등록대장",
        "authorName": "홍길동",
        "department": "기획과",
        "startDate": "2024-01-01",
        "endDate": "2025-12-31",
        "keyword": "적극행정",
    })
    req_id = created["requestId"]
    assert created["state"] == "pending"
    assert created["scope"]["authorName"] == "홍길동"

    status, body = call(service.port, "GET", "/bridge/v1/requests", token)
    assert status == 200
    assert body["project"] == "기획과 인수인계"
    assert [item["requestId"] for item in body["requests"]] == [req_id]

    # 진행 상태 보고 (running)
    status, updated = call(
        service.port,
        "POST",
        f"/bridge/v1/requests/{req_id}/progress",
        token,
        {"state": "running", "done": 15, "total": 42, "message": "1꾸러미 저장 완료"},
    )
    assert status == 200
    assert (updated["state"], updated["done"], updated["total"]) == ("running", 15, 42)

    # 완료 보고 (completed) → active 목록에서는 빠진다
    status, completed = call(
        service.port,
        "POST",
        f"/bridge/v1/requests/{req_id}/progress",
        token,
        {"state": "completed", "done": 42, "total": 42, "runId": RUN},
    )
    assert status == 200 and completed["state"] == "completed" and completed["runId"] == RUN
    assert call(service.port, "GET", "/bridge/v1/requests", token)[1]["requests"] == []
    assert service.requests()[0]["state"] == "completed"

    # 잘못된 요청 및 권한 검사
    assert call(service.port, "POST", "/bridge/v1/requests/missing_req/progress", token, {"state": "running"})[0] == 404
    assert call(service.port, "POST", f"/bridge/v1/requests/{req_id}/progress", token, {"state": "unknown"})[0] == 400
    assert call(service.port, "POST", f"/bridge/v1/requests/{req_id}/progress", token, {"done": -1})[0] == 400

    read_only = pairing.generate_token()
    service.store.add_client("b" * 32, pairing.hash_token(read_only), ("read",))
    conn = http.client.HTTPConnection("127.0.0.1", service.port, timeout=10)
    try:
        conn.request(
            "POST",
            f"/bridge/v1/requests/{req_id}/progress",
            body=json.dumps({"state": "running"}),
            headers={"Authorization": f"Bearer {read_only}", "Content-Type": "application/json"},
        )
        res = conn.getresponse()
        assert res.status == 403
    finally:
        conn.close()


def test_onboarding_view_creates_bridge_request_and_imports_onnara_package(service, token, downloads, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from app.ui.views.onboarding import OnboardingView

    qapp = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "project" / "project.db")
    db.init()
    bridge_service.install(service)
    try:
        view = OnboardingView(db)
        view.req_author.setText("홍길동")
        view.req_start_date.setText("2024-01-01")
        view.req_end_date.setText("2025-12-31")
        view.req_button.click()

        reqs = service.requests(active_only=True)
        assert len(reqs) == 1
        assert reqs[0]["scope"]["authorName"] == "홍길동"
        assert "수집 요청 대기 중 (홍길동)" in view.req_status.text()

        # 확장이 진행률을 보고한 뒤 온보딩 화면 동기화
        call(
            service.port,
            "POST",
            f"/bridge/v1/requests/{reqs[0]['requestId']}/progress",
            token,
            {"state": "running", "done": 15, "total": 42, "message": "문서등록대장 수집 중"},
        )
        view.sync_requests()
        assert "수집 중 15/42건" in view.req_status.text()

        # 온나라 꾸러미만 먼저 추가해도, 업무 분장을 적으면 분석 시작이 활성화된다
        view.charter_text.setPlainText("민원 처리 실태 점검")
        pkg = downloads / "onNara.sAIde" / "수집" / RUN
        make_package(pkg)
        view.add_onnara(pkg)
        assert view.start.isEnabled()
        assert "온나라 공문 3건 포함" in view.summary.text()
    finally:
        bridge_service.install(None)
        view.deleteLater()
        qapp.processEvents()
        db.close()
