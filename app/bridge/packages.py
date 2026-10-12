"""확장이 알려 온 꾸러미 폴더를 받아도 되는지 가린다. 소켓도 화면도 모른다.

확장은 꾸러미를 다 만들면 "이 폴더에 준비됐다"고 경로를 알려 온다. 경로는 밖에서 온
값이라 그대로 믿지 않는다.
  · 허용한 수신 폴더(기본: 이 사용자의 다운로드 폴더) 아래여야 한다.
  · 확장이 만드는 모양(`…/onNara.sAIde/수집/<실행ID>`)이어야 하고 실행 ID가 맞아야 한다.
  · 그 안의 package.json이 읽히고 같은 실행 ID를 밝혀야 한다.
알림은 받기만 한다. 자료로 등록할지는 사용자가 눈치코치 화면에서 정한다.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys

from ..connectors.onnara.saide_package import SaidePackageConnector
from .server import BridgeError

RUN_ID = re.compile(r"[A-Za-z0-9_-]{6,80}")
PACKAGE_PARENTS = ("onNara.sAIde", "수집")


def downloads_folder() -> Path | None:
    """이 사용자의 다운로드 폴더. 윈도우에서는 옮겨 둔 위치를 운영체제에 묻는다."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

            # FOLDERID_Downloads {374DE290-123F-4565-9164-39C4925E467B}
            folder_id = GUID(0x374DE290, 0x123F, 0x4565, (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
            pointer = ctypes.c_wchar_p()
            shell32 = ctypes.WinDLL("shell32")
            shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                                     ctypes.POINTER(ctypes.c_wchar_p)]
            if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(pointer)) == 0 and pointer.value:
                found = Path(pointer.value)
                ctypes.WinDLL("ole32").CoTaskMemFree(ctypes.cast(pointer, ctypes.c_void_p))
                return found
        except (OSError, AttributeError, ValueError):
            pass
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return Path(home) / "Downloads" if home else None


def resolve_package(path, run_id, roots: list[Path]) -> tuple[Path, SaidePackageConnector]:
    """알려 온 경로가 받아도 되는 꾸러미 폴더면 그 폴더와 읽은 꾸러미. 아니면 사유와 함께 거절한다."""
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise BridgeError(400, "invalid_request", "꾸러미 알림의 형식이 올바르지 않습니다.")
    if not isinstance(path, str) or not path or "\0" in path or path.startswith(("\\\\", "//")):
        raise BridgeError(400, "invalid_request", "꾸러미 알림의 형식이 올바르지 않습니다.")
    try:
        folder = Path(path)
        if not folder.is_absolute():
            raise ValueError
        folder = folder.resolve()
    except (OSError, ValueError):
        raise BridgeError(400, "invalid_request", "꾸러미 알림의 형식이 올바르지 않습니다.") from None
    if folder.name != run_id or tuple(part.lower() for part in folder.parts[-3:-1]) != tuple(
            part.lower() for part in PACKAGE_PARENTS):
        raise BridgeError(403, "path_rejected", "onNara.sAIde가 만든 꾸러미 폴더가 아닙니다.")
    allowed = []
    for root in roots:
        try:
            allowed.append(Path(root).resolve())
        except OSError:
            continue
    if not any(folder.is_relative_to(root) for root in allowed):
        raise BridgeError(403, "path_rejected",
                          "허용한 수신 폴더 밖입니다. 눈치코치 설정에서 수신 폴더를 더하거나, 꾸러미 폴더를 직접 고르세요.")
    try:
        connector = SaidePackageConnector(folder)
    except ValueError as exc:
        raise BridgeError(422, "package_unreadable", str(exc)) from None
    if connector.run_id != run_id:
        raise BridgeError(422, "package_unreadable", "꾸러미의 실행 ID가 알림과 다릅니다.")
    return folder, connector
