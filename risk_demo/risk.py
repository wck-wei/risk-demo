from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .configuration import CONTEXT_FIELDS, OUTPUT_FIELDS, Configuration
from .contracts import EngineRequest, Fault, Trace, matches_type
from .engine import Engine
from .features import FeatureService
from .model import ModelService
from .platforms import MagicCube, MetadataPlatform, RCMS

FEATURE_FAULTS = {"missing-once", "missing-always", "stale"}
FAULTS = FEATURE_FAULTS | {"none", "feature-timeout", "model-timeout", "version"}


class InvalidRequest(ValueError):
    pass


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def normalize(request, config, trace_id):
    if not isinstance(request, dict):
        raise InvalidRequest("请求必须是 JSON 对象")
    scene_id = request.get("scene")
    if not nonempty(scene_id) or scene_id not in config.scenes:
        raise InvalidRequest("scene 缺失或不受支持")
    scene = config.scenes[scene_id]
    entity_fields = ("customer_id", *scene["entity_fields"])
    required = ("request_id", "product_code", *entity_fields)
    for name in required:
        if not nonempty(request.get(name)):
            raise InvalidRequest(f"{name} 必须为非空字符串")
    for name in ("trace_id", "channel"):
        if name in request and not nonempty(request[name]):
            raise InvalidRequest(f"{name} 必须为非空字符串")

    amounts = [request[name] for name in ("amount", *scene["amount_aliases"]) if name in request]
    if not amounts or any(not matches_type(value, "number") or value <= 0 for value in amounts):
        raise InvalidRequest("amount 或场景金额别名必须为有限正数")
    if any(value != amounts[0] for value in amounts):
        raise InvalidRequest("amount 与场景金额别名不一致")
    term = request.get("term_months")
    if not matches_type(term, "integer") or term <= 0:
        raise InvalidRequest("term_months 必须为正整数")
    timeout = request.get("timeout_ms", 100)
    if not matches_type(timeout, "integer") or timeout <= 0:
        raise InvalidRequest("timeout_ms 必须为正整数")
    if "pricing" in scene and "base_rate" not in request:
        raise InvalidRequest("定价需要 base_rate")
    for name in ("base_rate", "competitor_rate"):
        if name in request and (not matches_type(request[name], "number") or not 0 <= request[name] <= 1):
            raise InvalidRequest(f"{name} 必须为 0 到 1 之间的比例")
    for name in ("available_credit", "coupon_amount", "monthly_income"):
        if name in request and (not matches_type(request[name], "number") or request[name] < 0):
            raise InvalidRequest(f"{name} 必须为有限非负数")
    try:
        if "request_time" in request:
            timestamp = datetime.fromisoformat(request["request_time"])
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            timestamp = timestamp.astimezone(timezone.utc)
        else:
            timestamp = datetime.now(timezone.utc)
        # Validate that mock snapshot timestamps can be represented for this feature set.
        timestamp - timedelta(seconds=config.features[scene["feature_set_id"]]["ttl"] + 1)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidRequest("request_time 必须为有效的 ISO 8601 时间") from exc
    context = {key: request[key] for key in sorted(CONTEXT_FIELDS) if key in request}
    context["amount"] = amounts[0]
    entity_keys = {key: request[key] for key in entity_fields}
    return EngineRequest(trace_id, scene_id, context, entity_keys, timestamp, timeout)


class RiskService:
    def __init__(self, config=None):
        self.config = config if config is not None else Configuration.load()
        self.rcms = RCMS(self.config)
        self.metadata = MetadataPlatform(self.config.metadata, self.config.features)
        self.feature_service = FeatureService(self.metadata, self.config.fixtures)
        self.predictor = ModelService(self.config.models, self.config.scenes, self.config.features)
        self.magic_cube = MagicCube(self.config, self.predictor)
        self.engine = Engine(self.config, self.feature_service, self.magic_cube)

    def decide(self, request, *, fault=Fault()):
        supplied_trace = request.get("trace_id") if isinstance(request, dict) else None
        trace_id = supplied_trace if nonempty(supplied_trace) else f"TR-{uuid4().hex}"
        output = dict.fromkeys(OUTPUT_FIELDS)
        try:
            normalized = normalize(request, self.config, trace_id)
            self._validate_fault(normalized.scene, fault)
        except InvalidRequest as exc:
            trace = Trace(trace_id, 0)
            trace.add("risk.received")
            trace.add("risk.response", decision="INVALID", code="PARAM_INVALID")
            return dict(code="PARAM_INVALID", message=str(exc), decision="INVALID",
                        trace_id=trace_id, reason_codes=["PARAM_INVALID"],
                        component_results=dict(engine=None, features=None, model=None,
                                               component_trace=trace.events), **output)
        trace = Trace(trace_id, normalized.timeout_ms)
        trace.add("risk.received", scene=normalized.scene, timeout_ms=normalized.timeout_ms)
        flow = self.rcms.flow(normalized.scene)
        trace.add("rcms.route", strategy_id=flow.strategy_id, flow_version=flow.version,
                  stages=[stage for stage, rules in flow.stages if rules])
        engine = self.engine.decide(normalized, trace, fault, flow)
        trace.add("risk.response", decision=engine.decision, code="OK")
        output.update(engine.output)
        if engine.model_result and engine.model_result.status == "OK":
            output["risk_level"] = engine.model_result.risk_level
        return dict(code="OK", message="决策完成", decision=engine.decision,
                    trace_id=trace_id, reason_codes=engine.reason_codes, **output,
                    component_results=dict(
                        engine=dict(strategy_id=flow.strategy_id, flow_version=flow.version, decision=engine.decision,
                                    hit_rules=engine.hit_rules, next_action=engine.next_action),
                        features=asdict(engine.feature_snapshot),
                        model=asdict(engine.model_result) if engine.model_result else None,
                        component_trace=engine.component_trace,
                    ))

    def _validate_fault(self, scene, fault):
        if fault.kind not in FAULTS:
            raise InvalidRequest("不支持的故障类型")
        if fault.kind in FEATURE_FAULTS:
            feature_set = self.config.features[self.config.scenes[scene]["feature_set_id"]]
            if fault.feature not in feature_set["features"]:
                raise InvalidRequest("故障特征必须属于当前场景")
        elif fault.feature is not None:
            raise InvalidRequest("此故障类型无需指定特征")
