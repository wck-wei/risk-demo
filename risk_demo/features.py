from datetime import timedelta

from .contracts import Fault, FeatureRequest, FeatureResult, matches_type


class FeatureService:
    def __init__(self, feature_sets, fixtures):
        self.feature_sets = feature_sets
        self.fixtures = fixtures

    def batch_get(self, request: FeatureRequest, fault: Fault, *, retry=False) -> FeatureResult:
        config = self.feature_sets[request.feature_set_id]
        version = "FS_INCOMPATIBLE" if fault.kind == "version" else config["version"]
        ttl = config["ttl"]
        latency = config["latency_ms"]
        timed_out = fault.kind == "feature-timeout" or request.timeout_ms < latency
        values, meta = {}, {}
        for name in request.feature_names:
            spec = config["features"][name]
            source = spec["source"]
            age = ttl + 1 if fault.kind == "stale" and fault.feature == name else min(60, ttl)
            calculated = request.as_of - timedelta(seconds=age)
            forced_missing = fault.feature == name and (
                fault.kind == "missing-always" or (fault.kind == "missing-once" and not retry)
            )
            record = (request.request_features if source == "request"
                      else self.fixtures[source].get(request.entity_keys.get(f"{source}_id"), {}))
            value = record.get(name)
            reason = None
            if timed_out:
                reason = "TIMEOUT"
            elif forced_missing or value is None:
                reason = "NOT_FOUND"
            elif not matches_type(value, spec["type"]):
                reason = "INVALID_TYPE"
            elif age > ttl:
                reason = "STALE"
            if reason is None:
                values[name] = value
            meta[name] = dict(source=source, calculated_at=None if timed_out else calculated.isoformat(),
                              version=version, missing=reason is not None, missing_reason=reason,
                              required=request.required_flags[name], defaulted=False)
        return FeatureResult(values, meta, [name for name in request.feature_names if name not in values],
                             version, ttl, request.timeout_ms if timed_out else latency,
                             "TIMEOUT" if timed_out else "OK")
