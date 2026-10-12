"""onNara.sAIde와 이 PC 안에서만 주고받는 로컬 브리지.

자료(본문·첨부)는 이 길로 오지 않는다 — 꾸러미 폴더로 온다. 브리지는 "꾸러미가
준비됐다"는 알림과 진행·보유 문서 조회 같은 제어만 맡는다
(docs/온나라_연동_구현계획서_2026-10-12.md §6).
"""
from .policy import API_VERSION, DEFAULT_PORT
from .server import BridgeError, BridgeServer, Client, Context, Route

__all__ = ["API_VERSION", "DEFAULT_PORT", "BridgeError", "BridgeServer", "Client", "Context", "Route"]
