"""Windows에 든 글자 인식(Windows.Media.Ocr)으로 그림의 글자를 읽는다.

로컬 모델이 그림을 읽지 못하는 PC가 있다. 실측에서 Ollama 0.40.3의 gemma4:e2b·e4b는
색은 맞혔지만 글자가 든 그림은 크기·형식·API·CPU/GPU를 가리지 않고 "행과 열이 많은
표"로만 보았다. 같은 PC에서 Windows의 글자 인식은 쪽지 캡처 한 장을 0.3초에 읽었다.

  · 설치할 것이 없다. Windows 10/11에 들어 있고, 한국어 인식은 한국어 Windows에 기본으로
    깔려 있다. 없으면 available()이 False이고 호출하는 쪽이 다른 길(로컬 모델)을 쓴다.
  · PC 밖으로 나가지 않는다. PowerShell로 이 PC의 인식기를 부를 뿐이다.
  · 스크립트를 파일로 두지 않고 명령줄 인자(-EncodedCommand)로 넘긴다. 실행 정책과 배포
    묶음에 걸리지 않는다. 그림은 임시 폴더에 두었다가 바로 지운다.
  · 인식기는 글을 낱말 상자로 돌려준다. 같은 높이의 상자를 한 줄로 모으고, 사이가 넓게
    벌어진 곳은 표의 칸으로 보고 ' | '로 나눈다 — 공문 목록 캡처의 한 행(문서번호 |
    보고일자 | 제목)이 한 줄이 된다.

결과는 원문이 아니라 인식기가 읽은 글이다. 오자가 섞인다('위촉' → '위족').
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ENGINE = "winocr"
TIMEOUT = 120.0
MAX_IMAGES = 20         # 한 번 부를 때 넘기는 그림 수
# 낱말 사이가 글자 높이의 이만큼 넘게 벌어지면 표의 다른 칸이다.
CELL_GAP = 1.6
# 두 상자의 세로 중심이 글자 높이의 이만큼 안에 있으면 같은 줄이다.
SAME_ROW = 0.6

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
function Await($operation, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
    $task.Wait(-1) | Out-Null
    $task.Result
}
$language = New-Object Windows.Globalization.Language 'ko'
if (-not [Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($language)) { 'NO-KOREAN'; exit 0 }
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
'READY'
foreach ($path in $paths) {
    try {
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $words = @()
        foreach ($line in $result.Lines) {
            foreach ($word in $line.Words) {
                $box = $word.BoundingRect
                $words += ,@($word.Text, [int]$box.X, [int]$box.Y, [int]$box.Width, [int]$box.Height)
            }
        }
        'PAGE ' + (ConvertTo-Json -InputObject @($words) -Compress -Depth 3)
        $stream.Dispose()
    } catch {
        'FAIL ' + $_.Exception.Message
    }
}
"""

_available: bool | None = None


def _run(paths: list[str]) -> list[str] | None:
    """PowerShell을 한 번 띄워 그림들을 읽힌다. 줄 목록, 띄우지 못하면 None."""
    if sys.platform != "win32":
        return None
    listed = ", ".join("'" + path.replace("'", "''") + "'" for path in paths)
    script = f"$paths = @({listed})\n{_SCRIPT}"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, timeout=TIMEOUT, creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", errors="replace").splitlines()


def available() -> bool:
    """이 PC에서 한국어 글자 인식을 쓸 수 있는가. 한 번 알아보고 기억한다."""
    global _available
    if os.environ.get("NUNCHICOACH_NO_WINOCR"):
        return False
    if _available is None:
        lines = _run([])
        _available = bool(lines) and "READY" in [line.strip() for line in lines]
    return _available


def lines_of(words: list[list]) -> list[str]:
    """낱말 상자들을 읽는 순서의 줄로 모은다. 넓게 벌어진 곳은 ' | '로 나눈다."""
    boxes = [(str(w[0]), int(w[1]), int(w[2]), int(w[3]), int(w[4])) for w in words if w and str(w[0]).strip()]
    boxes.sort(key=lambda box: (box[2] + box[4] / 2, box[1]))
    rows: list[list[tuple]] = []
    for box in boxes:
        middle, height = box[2] + box[4] / 2, max(box[4], 1)
        for row in reversed(rows[-4:]):
            row_middle = sum(b[2] + b[4] / 2 for b in row) / len(row)
            row_height = sum(b[4] for b in row) / len(row)
            if abs(middle - row_middle) <= max(height, row_height) * SAME_ROW:
                row.append(box)
                break
        else:
            rows.append([box])
    out = []
    for row in rows:
        row.sort(key=lambda box: box[1])
        height = sum(box[4] for box in row) / len(row)
        text = row[0][0]
        for before, box in zip(row, row[1:]):
            gap = box[1] - (before[1] + before[3])
            text += (" | " if gap > height * CELL_GAP else " ") + box[0]
        out.append(text)
    return out


def read_pngs(pngs: list[bytes]) -> list[str | None] | None:
    """그림마다 읽은 글(줄바꿈으로 이은 것). 못 읽은 그림은 None, 인식기를 못 쓰면 전체가 None."""
    if not pngs or not available():
        return None
    out: list[str | None] = []
    with tempfile.TemporaryDirectory(prefix="nunchi-ocr-") as folder:
        for start in range(0, len(pngs), MAX_IMAGES):
            paths = []
            for index, png in enumerate(pngs[start:start + MAX_IMAGES]):
                path = Path(folder) / f"{start + index}.png"
                path.write_bytes(png)
                paths.append(str(path))
            lines = _run(paths)
            if lines is None:
                return None
            pages = [line for line in lines if line.startswith(("PAGE ", "FAIL "))]
            for line in pages[:len(paths)]:
                if line.startswith("FAIL "):
                    out.append(None)
                    continue
                try:
                    words = json.loads(line[5:]) or []
                except ValueError:
                    out.append(None)
                    continue
                # 낱말이 하나뿐이면 ConvertTo-Json이 겹 배열을 한 겹으로 편다.
                if words and not isinstance(words[0], list):
                    words = [words]
                out.append("\n".join(lines_of(words)))
            out.extend([None] * (len(paths) - len(pages[:len(paths)])))
    return out
