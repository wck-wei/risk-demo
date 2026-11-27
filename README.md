# 风控链路模拟

用 Python 在单进程内演示授信、下单和定价。RCMS 发布规则流，特征网关按元信息平台的定义取数，Magic Cube 管理模型版本并提供在线接口。规则引擎和模型各自向特征网关查询所需数据。

## 浏览器演示

解压交付包，用浏览器打开 `demo/index.html`。无需启动 Python 服务。选择授信、下单或定价案例后，点击「播放流程」，或用「下一步」按讲解节奏逐步展开。点击三个发布平台或四个在线模块可查看职责与当前处理，流程结束后会显示结果和原因。

第一次演示可选择「标准授信」，接着切换到「反洗钱名单复核」「切换规则流版本」「上报后重新计算」或「切换模型版本」，观察流程和业务结果的变化。完整步骤见[前端演示指南](docs/demo.md)。

## 命令行与测试

命令行需要 Python 3.11 或更新版本。在解压后的 `risk-demo` 目录运行以下命令。

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
- [三平台配置与在线调用](docs/platforms.md)
- [开发前测试方案](docs/test-plan.md)
- [测试验证记录](docs/test-report.md)
