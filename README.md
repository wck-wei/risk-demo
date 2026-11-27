# 风控链路模拟

用 Python 在单进程内演示授信、下单和定价。风控入口负责校验和路由，引擎编排批量特征读取、规则、模型和补取。配置、特征数据与样例都是 JSON，适合离线演示与测试。

## 运行

需要 Python 3.11 或更新版本。解压后进入 `risk-demo` 目录，执行：

```sh
python3 -m risk_demo examples/credit.json --audit
python3 -m risk_demo examples/order.json --audit
python3 -m risk_demo examples/pricing.json --audit
python3 -m unittest discover -s tests -v
```

| 样例 | 决策 | 关键输出 |
| --- | --- | --- |
| `credit.json` | PASS | 额度 30000、12 期、低风险 |
| `order.json` | ALLOW | 放行金额 4999、免验证、低风险 |
| `pricing.json` | PRICE_OK | 最终利率 15%、演示 APR 16.2%、低风险 |

标准输出为决策 JSON。`--audit` 把同一 trace_id 下的审计事件按调用顺序输出到 stderr。业务决策的退出码为 `0`，请求参数错误为 `2`，配置加载失败为 `1`。金额沿用请求的单位，利率和概率均为小数比例。耗时为确定性的演示预算。

## 故障与 Python API

```sh
python3 -m risk_demo examples/credit.json --fault missing-once --feature income_level --audit
python3 -m risk_demo examples/credit.json --fault missing-always --feature income_level --audit
python3 -m risk_demo examples/order.json --fault stale --feature repay_capacity_score --audit
python3 -m risk_demo examples/order.json --fault model-timeout --audit
python3 -m risk_demo examples/credit.json --fault feature-timeout --audit
python3 -m risk_demo examples/pricing.json --fault version --audit
python3 -m risk_demo - < examples/credit.json
```

```python
import json
from pathlib import Path

from risk_demo.risk import RiskService

request = json.loads(Path("examples/credit.json").read_text(encoding="utf-8"))
response = RiskService().decide(request)
print(response["decision"], response["approved_amount"])
```

完整请求示例见 `examples/`。场景、特征、规则、模型配置以及 Mock 数据见 `risk_demo/config/`。配置在启动时加载，编辑后重新创建 `RiskService` 即可使用。测试使用独立的临时配置副本。

## 交付文档

- [需求与优化方案](docs/design.md)
- [开发前测试方案](docs/test-plan.md)
- [测试验证记录](docs/test-report.md)
