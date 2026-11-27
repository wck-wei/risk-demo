from dataclasses import dataclass
import json
from pathlib import Path

from .contracts import matches_type

CONFIG_DIR = Path(__file__).with_name("config")
RULE_OPERATORS = {"gt", "ge", "outside"}
CONTEXT_FIELDS = {"request_id", "product_code", "channel", "amount", "term_months",
                  "base_rate", "competitor_rate"}
FEATURE_SOURCES = {"customer", "device", "merchant", "request", "order_events"}
RULE_STAGES = {"AML", "BUSINESS", "FRAUD"}
NEXT_ACTION = {"PASS": "APPROVE_CREDIT", "ALLOW": "CONTINUE_ORDER", "PRICE_OK": "RETURN_PRICE",
               "REJECT": "STOP", "REVIEW": "MANUAL_REVIEW", "CHALLENGE": "VERIFY_ORDER"}
FALLBACK_ACTIONS = {"REVIEW", "REJECT", "CHALLENGE"}
OUTPUT_FIELDS = ("approved_amount", "approved_term", "final_rate", "apr",
                 "need_verify", "risk_level")


def feature_operands(operand):
    if isinstance(operand, list):
        for item in operand:
            yield from feature_operands(item)
    elif isinstance(operand, str) and operand.startswith("feature."):
        yield operand.removeprefix("feature.")


def validate_operand(operand, features):
    if isinstance(operand, list) and operand:
        return all(not isinstance(item, list) and validate_operand(item, features) for item in operand)
    if isinstance(operand, str):
        scope, _, name = operand.partition(".")
        return (scope == "feature" and name in features) or (
            scope == "request" and name in {"amount", "term_months"}
        )
    return matches_type(operand, "number")


@dataclass(frozen=True)
class Configuration:
    scenes: dict
    features: dict
    models: dict
    rules: dict
    fixtures: dict
    metadata: dict

    @classmethod
    def load(cls, directory=CONFIG_DIR):
        directory = Path(directory)
        try:
            data = {name: json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
                    for name in ("scenes", "features", "models", "rules", "fixtures", "metadata")}
            config = cls(**data)
            config.validate()
        except (OSError, KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError(f"配置无效 ({directory}): {exc}") from exc
        return config

    def validate(self):
        if not isinstance(self.scenes, dict) or not self.scenes:
            raise ValueError("至少需要一个场景")
        for source in ("customer", "device", "merchant"):
            records = self.fixtures[source]
            if not isinstance(records, dict) or not all(isinstance(record, dict) for record in records.values()):
                raise ValueError(f"{source}: 实体 Mock 数据必须是对象")
        if not isinstance(self.metadata["release"], str) or not self.metadata["release"].strip():
            raise ValueError("元信息发布版本无效")
        for source in FEATURE_SOURCES:
            definition = self.metadata["sources"][source]
            entity_key = None if source == "request" else ("customer_id" if source == "order_events" else f"{source}_id")
            allowed_routes = {"OUTER_API"} if source == "order_events" else {"LOCAL", "TABLE"}
            if source == "request":
                allowed_routes = {"LOCAL"}
            if (definition["route"] not in allowed_routes or definition["entity_key"] != entity_key
                    or not all(isinstance(definition[field], str) and definition[field].strip()
                               for field in ("domain", "label"))):
                raise ValueError(f"{source}: 元信息路由无效")
        for customer_id, records in self.fixtures["order_events"].items():
            if customer_id not in self.fixtures["customer"] or not isinstance(records, list):
                raise ValueError("订单事件客户或事件列表无效")
            for record in records:
                if any(not matches_type(record[field], "number") or record[field] < 0
                       for field in ("age_days", "amount")):
                    raise ValueError("订单事件时间或金额无效")
        for name, scene in self.scenes.items():
            feature_set = self.features[scene["feature_set_id"]]
            features = feature_set["features"]
            stages = scene["rule_stages"]
            if not scene["flow_version"] or not stages or not set(stages) <= RULE_STAGES or len(set(stages)) != len(stages):
                raise ValueError(f"{name}: RCMS 规则阶段无效或重复")
            if not set(scene["engine_features"]) <= features.keys():
                raise ValueError(f"{name}: 引擎引用未知特征")
            model = self.models[scene["model_code"]]
            if not isinstance(model["version"], str) or not model["version"].strip():
                raise ValueError(f"{name}: 模型版本无效")
            if model["feature_version"] != feature_set["version"]:
                raise ValueError(f"{name}: 配置的特征版本与模型版本要求不一致")
            if not model["weights"] or not model["weights"].keys() <= features.keys():
                raise ValueError(f"{name}: 模型引用了未知特征或特征为空")
            if model["kind"] not in {"credit", "fraud", "pricing"}:
                raise ValueError(f"{name}: 无效模型种类")
            if scene["fallback"] not in FALLBACK_ACTIONS or not scene["model_timeout_reason"]:
                raise ValueError(f"{name}: 无效降级动作或模型超时原因码")
            expected_actions = {
                "credit": ("PASS", "REVIEW"),
                "fraud": ("ALLOW", "CHALLENGE"),
                "pricing": ("PRICE_OK", "PRICE_OK"),
            }
            if (model["good"], model["bad"]) != expected_actions[model["kind"]]:
                raise ValueError(f"{name}: 模型决策动作与模型种类不一致")
            if model["kind"] != "pricing" and not model["reason"]:
                raise ValueError(f"{name}: 缺少模型未通过原因码")
            numeric = [model["bias"], model["threshold"], *model["weights"].values()]
            if not all(matches_type(value, "number") for value in numeric) or not 0 <= model["threshold"] <= 1:
                raise ValueError(f"{name}: 模型权重或阈值无效")
            if model["kind"] == "pricing":
                pricing = scene["pricing"]
                values = [pricing["floor"], pricing["cap"], pricing["apr_addon"], pricing["match_tolerance"],
                          model["low_premium"], model["high_premium"]]
                if not all(matches_type(value, "number") and 0 <= value <= 1 for value in values):
                    raise ValueError(f"{name}: 定价数值无效")
                if pricing["floor"] > pricing["cap"]:
                    raise ValueError(f"{name}: 利率下限高于上限")
            for value in (feature_set["ttl"], feature_set["latency_ms"], model["latency_ms"]):
                if type(value) is not int or value <= 0:
                    raise ValueError(f"{name}: TTL 或耗时必须为正整数")
            for field, spec in features.items():
                if spec["source"] not in FEATURE_SOURCES or spec["type"] not in {"number", "integer"}:
                    raise ValueError(f"{name}: {field} 的来源或类型无效")
                if spec["source"] == "order_events" and (
                    spec.get("aggregation") != "sum_amount" or type(spec.get("window_days")) is not int
                    or not 1 <= spec["window_days"] <= 365
                ):
                    raise ValueError(f"{name}: {field} 的计算窗口无效")
                if type(spec["critical"]) is not bool:
                    raise ValueError(f"{name}: {field} 的关键标记无效")
                if not spec["critical"] and not matches_type(spec.get("default"), spec["type"]):
                    raise ValueError(f"{name}: {field} 缺少有效默认值")
                if spec["source"] == "request" and field not in {"term_months", "competitor_rate", "base_rate"}:
                    raise ValueError(f"{name}: {field} 没有可用的请求来源")
            for decision, fields in scene["output"].items():
                if decision not in NEXT_ACTION or not isinstance(fields, list) or not set(fields) <= set(OUTPUT_FIELDS):
                    raise ValueError(f"{name}: 输出映射包含未知字段")
            rules = self.rules[scene["strategy_id"]]
            ids = set()
            for rule in rules:
                if rule["op"] not in RULE_OPERATORS:
                    raise ValueError(f"{name}: 不支持规则操作 {rule['op']}")
                if rule["stage"] not in stages or rule["id"] in ids:
                    raise ValueError(f"{name}: 规则阶段未启用或规则重复")
                ids.add(rule["id"])
                if not all(validate_operand(rule[key], features) for key in ("left", "right")):
                    raise ValueError(f"{name}: 规则 {rule['id']} 的操作数无效")
                if rule["action"] not in FALLBACK_ACTIONS or not rule["id"]:
                    raise ValueError(f"{name}: 规则动作或原因码无效")
                right = rule["right"]
                if rule["op"] == "outside" and (
                    not isinstance(right, list) or len(right) != 2
                    or not all(matches_type(value, "number") for value in right)
                    or right[0] > right[1]
                ):
                    raise ValueError(f"{name}: 规则 {rule['id']} 的区间无效")
