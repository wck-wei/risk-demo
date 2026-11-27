# 测试验证记录

验证环境为 Linux、Python 3.12.14，使用标准库。按需求分析、优化设计、测试计划、开发、验证的顺序执行。首轮自动化验证通过后，在同一测试计划中补充竞品价匹配和错误版本拦截的断言，再完成实现和全量回归。

| 范围 | 结果 |
| --- | --- |
| T01–T07，三场景、风险规则、阈值、额度、定价边界 | 通过 |
| T08–T16，参数、接口链路、回补、默认值、超时、版本、配置校验 | 通过 |
| T17–T19，命令行、并发隔离、未知实体和请求来源特征 | 通过 |
| T20，交付 ZIP 完整性、解压后回归和 README 启动命令 | 通过 |

最终复验包含 19 项自动化测试和 1 项交付包验收。解压后再次得到授信 PASS（30000、12 期）、下单 ALLOW（4999、免验证）、定价 PRICE_OK（15%、APR 16.2%）。模型缺失回补成功时有一条 feature.retry 事件，持续缺失时转人工。

```sh
python3 -m zipfile -t risk-demo.zip
python3 -m zipfile -e risk-demo.zip verification
cd verification/risk-demo
python3 -m unittest discover -s tests -v
python3 -m risk_demo examples/credit.json --audit
python3 -m risk_demo examples/order.json --audit
python3 -m risk_demo examples/pricing.json --audit
```

运行结果以 Python 3.12.14 实测为准。项目中的额度、概率、利率和 APR 均为模拟业务输出。
