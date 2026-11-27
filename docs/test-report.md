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

## HTML 演示验证

| 用例 | 结果 | 证据 |
| --- | --- | --- |
| W01 | 通过 | 运行案例生成器的 `--check`，页面的 10 个案例均与 Python 引擎执行结果一致 |
| W02 | 通过 | 检查 10 个案例的最终结果，包括规则拦截跳过模型、仅回补一次、模型超时和竞品匹配 |
| W03 | 通过 | 使用 jsdom 从 `file://` 加载页面，验证播放、暂停、单步、回看、重置、场景切换和流程节点高亮 |
| W04 | 部分验证 | 已验证文件入口、页面控件和减少动态效果逻辑。云浏览器启动失败，实际浏览器中的桌面与窄屏渲染、键盘焦点外观尚未验证 |
| W05 | 通过 | 原有 19 项 Python 测试通过，解包后重新验证数据生成、DOM 交互与 ZIP 完整性 |

前端功能以 Node.js 24、jsdom 26.1.0 的 DOM 测试结果为准。演示页面运行时从本地文件加载资源。
