from .contracts import Fault, ModelRequest, ModelResult, matches_type


class ModelService:
    def __init__(self, models, scenes, feature_sets):
        self.models = models
        self.scenes = scenes
        self.feature_sets = feature_sets

    def predict(self, request: ModelRequest, fault: Fault, *, model_config=None) -> ModelResult:
        config = self.models[request.model_code] if model_config is None else model_config
        threshold = request.threshold_profile["threshold"]
        expected_version = config["feature_version"]
        latency = config["latency_ms"]
        result = ModelResult("OK", threshold, config["version"], request.feature_version_used, latency)
        if fault.kind == "model-timeout" or request.timeout_ms < latency:
            result.status = "TIMEOUT"
            result.latency_ms = request.timeout_ms
            return result
        if request.model_version != config["version"]:
            result.status = "MODEL_VERSION_MISMATCH"
            return result
        if request.feature_version_used != expected_version:
            result.status = "FEATURE_VERSION_MISMATCH"
            return result
        specs = self.feature_sets[self.scenes[request.scene]["feature_set_id"]]["features"]
        for name in config["weights"]:
            metadata = request.feature_meta.get(name, {})
            if name not in request.feature_vector or not metadata:
                result.missing_features.append(name)
            elif metadata.get("version") != expected_version:
                result.status = "FEATURE_VERSION_MISMATCH"
                return result
            elif not matches_type(request.feature_vector[name], specs[name]["type"]):
                result.status = "INVALID_FEATURE_TYPE"
                return result
        if result.missing_features:
            result.status = "MISSING_FEATURES"
            return result
        raw_score = config["bias"] + sum(weight * request.feature_vector[name]
                                         for name, weight in config["weights"].items())
        result.score = round(min(1, max(0, raw_score)), 6)
        fraud = config["kind"] == "fraud"
        good = result.score < threshold if fraud else result.score >= threshold
        result.probability = result.score if fraud else round(1 - result.score, 6)
        result.risk_level = "LOW" if good else "HIGH"
        result.decision_hint = config["good"] if good else config["bad"]
        if not good:
            result.reason_code = config["reason"]
        if config["kind"] == "pricing":
            result.risk_premium = config["low_premium"] if good else config["high_premium"]
            result.suggested_rate = round(request.business_context["base_rate"] + result.risk_premium, 6)
        return result
