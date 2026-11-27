from decimal import Decimal
from operator import ge, gt

from .configuration import NEXT_ACTION, feature_operands
from .contracts import EngineRequest, EngineResult, Fault, FeatureRequest, ModelRequest, Trace

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
    def __init__(self, config, feature_service, model_service):
        self.config = config
        self.feature_service = feature_service
        self.model_service = model_service

    def decide(self, request: EngineRequest, trace: Trace, fault: Fault) -> EngineResult:
        scene = self.config.scenes[request.scene]
        feature_set = self.config.features[scene["feature_set_id"]]
        model_config = self.config.models[scene["model_code"]]
        rules = self.config.rules[request.strategy_id]
        trace.add("engine.strategy", strategy_id=request.strategy_id, feature_set_id=scene["feature_set_id"],
                  model_code=scene["model_code"])
        reasons, hits = [], []
        model = None

        def get_features(names, *, retry=False):
            specs = feature_set["features"]
            feature_request = FeatureRequest(
                request.trace_id, request.scene, request.entity_keys, scene["feature_set_id"], tuple(names),
                {name: specs[name]["critical"] for name in names}, request.as_of, trace.remaining,
                {name: request.business_context[name] for name in names
                 if specs[name]["source"] == "request" and name in request.business_context},
            )
            result = self.feature_service.batch_get(feature_request, fault, retry=retry)
            defaulted = []
            for name in result.missing_features:
                if not specs[name]["critical"]:
                    result.feature_values[name] = specs[name]["default"]
                    result.feature_meta[name].update(defaulted=True, default_source="feature_config")
                    defaulted.append(name)
            if defaulted:
                reasons.append("FEATURE_TIMEOUT_FALLBACK" if result.status == "TIMEOUT" else "FEATURE_MISSING")
            trace.add("feature.retry" if retry else "feature.batch_get", result.latency_ms,
                      status=result.status, names=list(names), missing_features=result.missing_features,
                      defaulted=defaulted, feature_version=result.feature_version)
            return result

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
            reason_codes = list(dict.fromkeys([*reasons, *extra_reasons]))
            trace.add("engine.decision", decision=decision, reason_codes=reason_codes)
            return EngineResult(decision, reason_codes, hits, model, snapshot, trace.events,
                                NEXT_ACTION[decision], output)

        def check_rules():
            nonlocal hits
            hits, blocked = evaluate_rules(rules, snapshot.feature_values, request.business_context)
            trace.add("engine.rules", hit_rules=[hit["rule_id"] for hit in hits], missing_features=blocked)
            if hits:
                return finish(hits[0]["action"], *(hit["rule_id"] for hit in hits))
            if blocked:
                return finish(scene["fallback"], "FEATURE_MISSING")
            return None

        def predict():
            names = model_config["weights"]
            model_request = ModelRequest(
                request.trace_id, request.scene, scene["model_code"], model_config["version"],
                snapshot.feature_version,
                {name: snapshot.feature_values[name] for name in names if name in snapshot.feature_values},
                {name: snapshot.feature_meta[name] for name in names},
                request.business_context, {"threshold": model_config["threshold"]}, trace.remaining,
            )
            result = self.model_service.predict(model_request, fault)
            trace.add("model.predict", result.latency_ms, status=result.status, model_version=result.model_version,
                      feature_version_used=result.feature_version_used, missing_features=result.missing_features,
                      decision_hint=result.decision_hint, score=result.score)
            return result

        snapshot = get_features(tuple(feature_set["features"]))
        timed_out = snapshot.status == "TIMEOUT"
        if timed_out and any(feature_set["features"][name]["critical"] for name in snapshot.missing_features):
            return finish(scene["fallback"], "FEATURE_TIMEOUT_FALLBACK")
        if snapshot.feature_version != model_config["feature_version"]:
            snapshot.status = "FEATURE_VERSION_MISMATCH"
            return finish(scene["fallback"], "FEATURE_VERSION_MISMATCH")
        rule_result = check_rules()
        if rule_result:
            return rule_result
        model = predict()
        if model.status == "MISSING_FEATURES":
            reasons.append("MODEL_MISSING_FEATURE")
            retry = get_features(tuple(model.missing_features), retry=True)
            for name in retry.feature_meta:
                snapshot.feature_values.pop(name, None)
            snapshot.feature_values.update(retry.feature_values)
            snapshot.feature_meta.update(retry.feature_meta)
            snapshot.missing_features = [name for name, meta in snapshot.feature_meta.items() if meta["missing"]]
            snapshot.latency_ms += retry.latency_ms
            if retry.status == "TIMEOUT":
                snapshot.status = "TIMEOUT"
                return finish(scene["fallback"], "FEATURE_TIMEOUT_FALLBACK")
            if retry.feature_version != snapshot.feature_version:
                snapshot.status = "FEATURE_VERSION_MISMATCH"
                return finish(scene["fallback"], "FEATURE_VERSION_MISMATCH")
            rule_result = check_rules()
            if rule_result:
                return rule_result
            model = predict()
        if model.status == "TIMEOUT":
            return finish(scene["fallback"], scene["model_timeout_reason"])
        if model.status == "MISSING_FEATURES":
            return finish(scene["fallback"], "MODEL_MISSING_FEATURE")
        if model.status != "OK":
            return finish(scene["fallback"], model.status)
        if model.decision_hint == "PRICE_OK":
            return finish("PRICE_OK", f"{model.risk_level}_RISK_PREMIUM")
        if model.decision_hint == model_config["bad"]:
            return finish(model.decision_hint, model_config["reason"])
        return finish(model.decision_hint)
