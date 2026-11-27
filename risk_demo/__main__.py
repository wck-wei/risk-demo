import argparse
import json
from pathlib import Path
import sys

from .contracts import Fault
from .risk import FAULTS, RiskService


def main():
    parser = argparse.ArgumentParser(description="执行一次风控模拟")
    parser.add_argument("request", help="JSON 请求文件，- 表示 stdin")
    parser.add_argument("--audit", action="store_true", help="将审计事件写入 stderr")
    parser.add_argument("--fault", default="none", choices=sorted(FAULTS))
    parser.add_argument("--feature", help="缺失或过期故障的特征名称")
    args = parser.parse_args()
    try:
        service = RiskService()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        source = sys.stdin.read() if args.request == "-" else Path(args.request).read_text(encoding="utf-8")
        request = json.loads(source)
    except (OSError, ValueError):
        response = service.decide(None)
        response["message"] = "无法读取有效的 JSON 请求"
    else:
        response = service.decide(request, fault=Fault(args.fault, args.feature))
    if args.audit:
        for event in response["component_results"]["component_trace"]:
            print(json.dumps(event, ensure_ascii=False), file=sys.stderr)
    print(json.dumps(response, ensure_ascii=False, allow_nan=False, indent=2))
    return 2 if response["code"] == "PARAM_INVALID" else 0


if __name__ == "__main__":
    sys.exit(main())
