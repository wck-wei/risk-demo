from dataclasses import dataclass
import json
from pathlib import Path

from .contracts import matches_type

CONFIG_DIR = Path(__file__).with_name("config")
RULE_OPERATORS = {"gt", "ge", "outside"}
CONTEXT_FIELDS = {"request_id", "product_code", "channel", "amount", "term_months",
                  "base_rate", "competitor_rate"}
FEATURE_SOURCES = {"customer", "device", "merchant", "request"}
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

    @classmethod
    def load(cls, directory=CONFIG_DIR):
        directory = Path(directory)
        try:
            data = {name: json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
                    for name in ("scenes", "features", "models", "rules", "fixtures")}
            config = cls(**data)
            config.validate()
        except (OSError, KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError(f"配置无效 ({directory}): {exc}") from exc
        return config

    def validate(self):
        if not isinstance(self.scenes, dict) or not self.scenes:
            raise ValueError("至少需要一个场景")
        for source in FEATURE_SOURCES - {"request"}:
            records = self.fixtures[source]
            if not isinstance(records, dict) or not all(isinstance(record, dict) for record in records.values()):
                raise ValueError(f"{source}: 实体 Mock 数据必须是对象")
        for name, scene in self.scenes.items():
            feature_set = self.features[scene["feature_set_id"]]
            features = feature_set["features"]
            model = self.models[scene["model_code"]]
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
                if type(spec["critical"]) is not bool:
                    raise ValueError(f"{name}: {field} 的关键标记无效")
                if not spec["critical"] and not matches_type(spec.get("default"), spec["type"]):
                    raise ValueError(f"{name}: {field} 缺少有效默认值")
                if spec["source"] == "request" and field not in {"term_months", "competitor_rate", "base_rate"}:
                    raise ValueError(f"{name}: {field} 没有可用的请求来源")
            for decision, fields in scene["output"].items():
                if decision not in NEXT_ACTION or not isinstance(fields, list) or not set(fields) <= set(OUTPUT_FIELDS):
                    raise ValueError(f"{name}: 输出映射包含未知字段")
            for rule in self.rules[scene["strategy_id"]]:
                if rule["op"] not in RULE_OPERATORS:
                    raise ValueError(f"{name}: 不支持规则操作 {rule['op']}")
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
