"""브리지 — 꾸러미 준비 알림과 보유 문서 조회.

여기서 지키는 것: 알려 온 경로를 그대로 믿지 않는다, 알림만으로는 아무것도 등록하지 않는다,
빠진 파일이 있는 문서를 '가진 것'으로 치지 않는다.
"""

import http.client
import json
import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from test_saide_package import make_package, write_package  # noqa: E402

from app.bridge import pairing, service as bridge_service
from app.bridge.packages import downloads_folder
from app.bridge.service import BridgeService
from app.bridge.store import AUDIT_NAME, BridgeStore
from app.core import onnara_import
from app.db import Database

EXTENSION = "a" * 32
RUN = "20261012-153000-test"            # make_package가 쓰는 실행 ID
ORIGIN = "http://onnara.invalid"


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
    service = BridgeService(store)
    service.describe = lambda: {"project": "시험 인수인계"}
    assert service.set_enabled(True)
    yield service
    service.stop()


@pytest.fixture
def token(service):
    status, body = call(service.port, "POST", "/bridge/v1/pair", body={"code": service.new_code().code, "label": "시험"})
    assert status == 200 and body["scopes"] == ["read", "write"]
    return body["token"]


@pytest.fixture
def package(downloads):
    folder = downloads / "onNara.sAIde" / "수집" / RUN
    make_package(folder)
    return folder


def announce(service, token, path, run_id=RUN):
    return call(service.port, "POST", "/bridge/v1/packages", token, {"runId": run_id, "path": str(path)})


# ── 알림 ────────────────────────────────────────────────────────────
def test_announced_package_waits_for_the_user_and_registers_nothing(service, token, package):
    status, body = announce(service, token, package)
    assert (status, body) == (202, {"runId": RUN, "state": "queued", "documents": 3, "files": 6, "project": "시험 인수인계"})
    assert call(service.port, "GET", f"/bridge/v1/packages/{RUN}", token)[1]["state"] == "queued"
    [arrival] = service.take_arrivals()
    assert arrival["path"] == str(package.resolve()) and arrival["documents"] == 3
    assert service.take_arrivals() == []                               # 한 번만 묻는다
    assert announce(service, token, package)[1]["state"] == "asked"    # 묻는 중에 다시 알려도 또 묻지 않는다
    service.settle_arrival(RUN, "dismissed")
    assert announce(service, token, package)[1]["state"] == "queued"   # 닫은 뒤 다시 알리면 다시 묻는다
    service.take_arrivals()
    service.settle_arrival(RUN, "imported")
    assert announce(service, token, package)[1]["state"] == "imported"
    assert call(service.port, "GET", "/bridge/v1/packages/never_announced", token)[0] == 404
    audit = service.store.path.with_name(AUDIT_NAME).read_text(encoding="utf-8")
    assert '"action": "package"' in audit and str(package) not in audit    # 경로는 기록에 남기지 않는다


def test_announce_needs_the_write_scope_and_a_token(service, token, package):
    assert announce(service, None, package)[1]["code"] == "unauthorized"
    read_only = pairing.generate_token()
    service.store.add_client("b" * 32, pairing.hash_token(read_only), ("read",))
    connection = http.client.HTTPConnection("127.0.0.1", service.port, timeout=10)
    try:
        connection.request("POST", "/bridge/v1/packages", body=json.dumps({"runId": RUN, "path": str(package)}),
                           headers={"Authorization": f"Bearer {read_only}", "Content-Type": "application/json"})
        response = connection.getresponse()
        assert (response.status, json.loads(response.read())["code"]) == (403, "scope_denied")
    finally:
        connection.close()
    assert service.take_arrivals() == []


def test_paths_the_extension_would_not_produce_are_refused(service, token, package, tmp_path, downloads):
    outside = tmp_path / "elsewhere" / "onNara.sAIde" / "수집" / RUN
    make_package(outside)
    wrong_shape = downloads / "아무폴더" / RUN
    make_package(wrong_shape)
    empty = downloads / "onNara.sAIde" / "수집" / "20261012_000000_none"
    empty.mkdir(parents=True)
    other = downloads / "onNara.sAIde" / "수집" / "20261012_111111_abcd"
    make_package(other)                                                # package.json의 실행 ID는 RUN이다
    cases = [
        (outside, RUN, 403, "path_rejected"),                           # 수신 폴더 밖
        (wrong_shape, RUN, 403, "path_rejected"),                       # 확장이 만드는 모양이 아니다
        (package.parent, RUN, 403, "path_rejected"),                    # 실행 ID 폴더가 아니다
        (empty, empty.name, 422, "package_unreadable"),                 # package.json이 없다
        (other, other.name, 422, "package_unreadable"),                 # 실행 ID가 다르다
        (package, "../" + RUN, 400, "invalid_request"),
        ("onNara.sAIde/수집/" + RUN, RUN, 400, "invalid_request"),      # 상대 경로
        ("\\\\server\\share\\onNara.sAIde\\수집\\" + RUN, RUN, 400, "invalid_request"),
        (str(package) + "\\..\\..\\..\\..\\elsewhere\\onNara.sAIde\\수집\\" + RUN, RUN, 403, "path_rejected"),
    ]
    for path, run_id, status, code in cases:
        got = announce(service, token, path, run_id)
        assert (got[0], got[1]["code"]) == (status, code), (path, got)
    assert service.take_arrivals() == []
    assert call(service.port, "POST", "/bridge/v1/packages", token, {"runId": RUN})[1]["code"] == "invalid_request"


def test_downloads_folder_is_the_default_and_extra_roots_must_be_local(tmp_path):
    assert downloads_folder() is not None and downloads_folder().is_absolute()
    store = BridgeStore(tmp_path / "bridge.json")
    service = BridgeService(store)
    assert service.receive_roots() == [downloads_folder()]
    store.set_receive_roots([str(tmp_path / "받은파일"), str(tmp_path / "받은파일")])
    assert BridgeStore(store.path).receive_roots == [str(tmp_path / "받은파일")]
    for bad in ("\\\\server\\share", "relative\\folder"):
        with pytest.raises(ValueError):
            store.set_receive_roots([bad])


# ── 보유 문서 ───────────────────────────────────────────────────────
def test_known_means_fully_collected_in_the_open_project(service, token, package, tmp_path):
    ids = {"origin": ORIGIN, "ids": ["row-1", "row-2", "row-3", "row-9"]}
    assert call(service.port, "POST", "/bridge/v1/known", token, ids)[1]["code"] == "no_project"

    data = json.loads((package / "package.json").read_text(encoding="utf-8"))
    data["documents"][1]["assets"][1] = {"asset_id": "a2", "role": "attachment", "filename": "붙임.xlsx", "status": "failed"}
    write_package(package, data)                                        # row-2는 붙임 하나를 받지 못했다
    db = Database(tmp_path / "project" / "project.db")
    db.init()
    try:
        onnara_import.add_to_project(db, package)
    finally:
        db.close()
    service.project_db = tmp_path / "project" / "project.db"

    status, body = call(service.port, "POST", "/bridge/v1/known", token, ids)
    assert status == 200 and sorted(body["known"]) == ["row-1", "row-3"] and body["project"] == "시험 인수인계"
    # 다른 온나라(다른 주소)의 같은 식별자는 가진 것이 아니다.
    assert call(service.port, "POST", "/bridge/v1/known", token, {"origin": "http://other.invalid", "ids": ["row-1"]})[1]["known"] == []
    assert call(service.port, "POST", "/bridge/v1/known", token, {"origin": ORIGIN, "ids": []})[1]["known"] == []
    for bad in ({"ids": ["row-1"]}, {"origin": ORIGIN, "ids": "row-1"}, {"origin": ORIGIN, "ids": [1]},
                {"origin": ORIGIN, "ids": ["x"] * 501}):
        assert call(service.port, "POST", "/bridge/v1/known", token, bad)[1]["code"] == "invalid_request"
    # 조회는 읽기 전용이다 — 프로젝트 DB를 고치지 않는다.
    injected = {"origin": ORIGIN, "ids": ["row-1'; DROP TABLE source_records; --"]}
    assert call(service.port, "POST", "/bridge/v1/known", token, injected)[1]["known"] == []
    assert sorted(call(service.port, "POST", "/bridge/v1/known", token, ids)[1]["known"]) == ["row-1", "row-3"]


# ── 화면 ────────────────────────────────────────────────────────────
def test_window_imports_an_announced_package_only_when_the_user_agrees(service, token, package, tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from app.ui.shell import MainWindow
    qapp = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "project" / "project.db")
    db.init()
    window = MainWindow(db, project_name="시험 인수인계")
    started = []
    monkeypatch.setattr(window, "_start_pipeline", lambda *a, **k: started.append(True))
    asked = []
    bridge_service.install(service)
    try:
        announce(service, token, package)
        window._ask_about_arrivals()                                   # 창이 보이지 않으면 묻지 않는다
        assert service.take_arrivals() != [] and db.work_sources() == []

        service.settle_arrival(RUN, "dismissed")
        announce(service, token, package)
        monkeypatch.setattr(window, "isVisible", lambda: True)
        monkeypatch.setattr(window, "_confirm_arrival", lambda arrival: (asked.append(arrival["path"]), False)[-1])
        window._ask_about_arrivals()
        assert asked == [str(package.resolve())] and db.work_sources() == [] and started == []     # 거절하면 아무 일도 없다
        assert call(service.port, "GET", f"/bridge/v1/packages/{RUN}", token)[1]["state"] == "dismissed"

        announce(service, token, package)
        monkeypatch.setattr(window, "_confirm_arrival", lambda arrival: True)
        window._ask_about_arrivals()
        assert [row["kind"] for row in db.work_sources()] == ["onnara"] and started == [True]
        assert call(service.port, "GET", f"/bridge/v1/packages/{RUN}", token)[1]["state"] == "imported"
        window._ask_about_arrivals()
        assert started == [True]                                       # 같은 꾸러미를 두 번 등록하지 않는다
    finally:
        bridge_service.install(None)
        window.shutdown()
        window.close()
        window.deleteLater()
        qapp.processEvents()
        db.close()
