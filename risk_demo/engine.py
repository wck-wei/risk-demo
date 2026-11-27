from decimal import Decimal
from operator import ge, gt

from .configuration import NEXT_ACTION, feature_operands
from .contracts import EngineRequest, EngineResult, Fault, Trace
from .features import FeatureSession

OPERATORS = {"gt": gt, "ge": ge, "outside": lambda value, bounds: not bounds[0] <= value <= bounds[1]}


def operand_value(operand, features, context):
    if isinstance(operand, list):
        return sum(operand_value(item, features, context) for item in operand)
    if isinstance(operand, str):
        scope, name = operand.split(".", 1)
        return (features if scope == "feature" else context)[name]
    return operand


def evaluate_rules(rules, features, context):
    hits, blocked = [], []
    for rule in rules:
        inputs = set(feature_operands(rule["left"])) | set(feature_operands(rule["right"]))
        unavailable = sorted(inputs - features.keys())
        if unavailable:
            blocked.extend(unavailable)
            continue
        left = operand_value(rule["left"], features, context)
        right = (rule["right"] if rule["op"] == "outside"
                 else operand_value(rule["right"], features, context))
        if OPERATORS[rule["op"]](left, right):
            hits.append({"rule_id": rule["id"], "action": rule["action"]})
    return hits, sorted(set(blocked))


class Engine:
    def __init__(self, config, feature_service, magic_cube):
        self.config = config
        self.feature_service = feature_service
        self.magic_cube = magic_cube

    def decide(self, request: EngineRequest, trace: Trace, fault: Fault, flow) -> EngineResult:
        scene = self.config.scenes[request.scene]
        session = FeatureSession(self.feature_service, request, scene["feature_set_id"], trace, fault)
        feature_names = set(flow.engine_features)
        for _, rules in flow.stages:
            for rule in rules:
                feature_names.update(feature_operands(rule["left"]))
                feature_names.update(feature_operands(rule["right"]))
        engine_features = tuple(name for name in session.config["features"] if name in feature_names)
        trace.add("engine.strategy", strategy_id=flow.strategy_id, flow_version=flow.version,
                  feature_set_id=scene["feature_set_id"], model_code=scene["model_code"])
        snapshot = session.snapshot
        hits = []
        model = None

        def finish(decision, *extra_reasons):
            context = request.business_context
            values = dict(approved_amount=context["amount"], approved_term=context["term_months"],
                          need_verify=decision == "CHALLENGE",
                          risk_level=model.risk_level if model and model.status == "OK" else None)
            if decision == "PRICE_OK":
                pricing = scene["pricing"]
                floor = Decimal(str(pricing["floor"]))
                cap = Decimal(str(pricing["cap"]))
                rate = Decimal(str(context["base_rate"])) + Decimal(str(model.risk_premium))
                rate = min(cap, max(floor, rate))
                competitor_meta = snapshot.feature_meta.get("competitor_rate", {})
                if model.risk_level == "LOW" and competitor_meta and not competitor_meta["missing"]:
                    competitor = Decimal(str(snapshot.feature_values["competitor_rate"]))
                    tolerance = Decimal(str(pricing["match_tolerance"]))
                    if floor <= competitor < rate and rate - competitor <= tolerance:
                        rate = competitor
                        extra_reasons += ("COMPETITOR_RATE_MATCHED",)
                values.update(final_rate=float(rate), apr=float(rate + Decimal(str(pricing["apr_addon"]))))
            output = {field: values.get(field) for field in scene["output"].get(decision, [])}
            reason_codes = list(dict.fromkeys([*session.reasons, *extra_reasons]))
            trace.add("engine.decision", decision=decision, reason_codes=reason_codes)
            return EngineResult(decision, reason_codes, hits, model, snapshot, trace.events,
                                NEXT_ACTION[decision], output)

        if engine_features:
            batch = session.fetch(engine_features, consumer="engine")
            if batch.status == "TIMEOUT":
                return finish(scene["fallback"], "FEATURE_TIMEOUT_FALLBACK")
            if batch.feature_version != session.config["version"]:
                snapshot.status = "FEATURE_VERSION_MISMATCH"
                return finish(scene["fallback"], "FEATURE_VERSION_MISMATCH")
        for stage, rules in flow.stages:
            if not rules:
                continue
            hits, blocked = evaluate_rules(rules, snapshot.feature_values, request.business_context)
            trace.add("engine.rules", stage=stage, hit_rules=[hit["rule_id"] for hit in hits],
                      missing_features=blocked)
            if hits:
                return finish(hits[0]["action"], *(hit["rule_id"] for hit in hits))
            if blocked:
                return finish(scene["fallback"], "FEATURE_MISSING")

        model = self.magic_cube.predict(scene["model_code"], request, session, trace, fault)
        if model.status == "TIMEOUT":
            return finish(scene["fallback"], scene["model_timeout_reason"])
        if model.status == "FEATURE_TIMEOUT":
            return finish(scene["fallback"], "FEATURE_TIMEOUT_FALLBACK")
        if model.status == "MISSING_FEATURES":
            return finish(scene["fallback"], "MODEL_MISSING_FEATURE")
        if model.status != "OK":
            return finish(scene["fallback"], model.status)
        if model.decision_hint == "PRICE_OK":
            return finish("PRICE_OK", f"{model.risk_level}_RISK_PREMIUM")
        if model.reason_code:
            return finish(model.decision_hint, model.reason_code)
        return finish(model.decision_hint)
