"""sAIde 꾸러미 폴더를 온나라 수집본으로 만든다. 온나라에 접속하지 않는다.

  python -m app.tools.onnara_package --package <꾸러미 폴더> --output <수집 폴더>
  python -m app.tools.onnara_package --package <꾸러미 폴더> --output <수집 폴더> --project <프로젝트 폴더>

--project를 주면 수집한 파일을 그 프로젝트의 자료원으로 등록하고 읽는다.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..connectors.onnara import SaidePackageConnector
from ..jobs.onnara_collect import Collection
from .onnara_demo import index_collection


def main(argv=None):
    parser = argparse.ArgumentParser(description="sAIde 꾸러미 → 온나라 수집본")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", type=Path)
    args = parser.parse_args(argv)
    try:
        connector = SaidePackageConnector(args.package)
        state = Collection(connector, connector.scope(), args.output).run()
        result = {"status": state["status"], **state["summary"], "package": state.get("package", {}),
                  "listing_errors": state.get("listing_errors", []),
                  "metadata_errors": state.get("metadata_errors", [])}
        if args.project and state["summary"]["downloaded"]:
            result["index"] = index_collection(state, args.output.resolve(), args.project.resolve())
        print(json.dumps(result, ensure_ascii=True, indent=2))
        # 이름 기준 꾸러미는 완전성을 unknown으로 둔다. 실패는 partial과 수집 중단뿐이다.
        return 0 if state["status"] == "finished" and state["summary"]["completeness"] != "partial" else 1
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
