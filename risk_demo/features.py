from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Lock

from .contracts import Fault, FeatureRequest, FeatureResult, matches_type


class FeatureService:
    """Route a batch using the published catalog and combine provider results."""

    def __init__(self, metadata, fixtures):
        self.metadata = metadata
        self.fixtures = fixtures
        self._orders = {}
        self._lock = Lock()

    def report_order(self, customer_id, event_id, amount, occurred_at):
        if (not isinstance(customer_id, str) or customer_id not in self.fixtures["customer"]
                or not isinstance(event_id, str) or not event_id.strip()):
            raise ValueError("订单上报需要已知客户和事件编号")
        if not matches_type(amount, "number") or amount <= 0:
            raise ValueError("订单上报金额必须为有限正数")
        if not isinstance(occurred_at, datetime) or occurred_at.utcoffset() is None:
            raise ValueError("订单上报时间需包含时区")
        event = (amount, occurred_at.astimezone(timezone.utc))
        with self._lock:
            orders = self._orders.setdefault(customer_id, {})
            old = orders.setdefault(event_id, event)
            if old != event:
                raise ValueError("订单事件已存在且内容不一致")

    def _compute(self, request, spec, customer_id):
        if customer_id not in self.fixtures["customer"]:
            return None
        window = spec["window_days"]
        seeded = self.fixtures["order_events"].get(customer_id, [])
        with self._lock:
            reported = tuple(self._orders.get(customer_id, {}).values())
        amounts = [event["amount"] for event in seeded if 0 < event["age_days"] <= window]
        amounts.extend(amount for amount, occurred_at in reported
                       if timedelta(0) < request.as_of - occurred_at <= timedelta(days=window))
        return float(sum((Decimal(str(amount)) for amount in amounts), Decimal(0)))

    def _read(self, request, name, spec, source, route):
        entity = request.entity_keys.get(source["entity_key"])
        if route == "OUTER_API":
            return self._compute(request, spec, entity)
        if spec["source"] == "request":
            return request.request_features.get(name)
        return self.fixtures[spec["source"]].get(entity, {}).get(name)

    def batch_get(self, request: FeatureRequest, fault: Fault, *, retry=False) -> FeatureResult:
        config = self.metadata.definitions(request.feature_set_id)
        version = "FS_INCOMPATIBLE" if fault.kind == "version" else config["version"]
        ttl = config["ttl"]
        latency = config["latency_ms"]
        timed_out = fault.kind == "feature-timeout" or request.timeout_ms < latency
        values, meta = {}, {}
        groups = defaultdict(list)
        for name in request.feature_names:
            spec = config["features"][name]
            source = self.metadata.source(spec["source"])
            groups[source["domain"], source["route"]].append((name, spec, source))
        for (domain, route), definitions in groups.items():
            for name, spec, source in definitions:
                age = ttl + 1 if fault.kind == "stale" and fault.feature == name else (
                    min(60, ttl) if route == "TABLE" else 0
                )
                forced_missing = fault.feature == name and (
                    fault.kind == "missing-always" or (fault.kind == "missing-once" and not retry)
                )
                reason = None
                if timed_out:
                    reason = "TIMEOUT"
                elif forced_missing:
                    reason = "NOT_FOUND"
                elif age > ttl:
                    reason = "STALE"
                else:
                    value = self._read(request, name, spec, source, route)
                    if value is None:
                        reason = "NOT_FOUND"
                    elif not matches_type(value, spec["type"]):
                        reason = "INVALID_TYPE"
                    else:
                        values[name] = value
                timestamp = None if timed_out else (request.as_of - timedelta(seconds=age)).isoformat()
                meta[name] = dict(source=spec["source"], calculated_at=timestamp, version=version,
                                  missing=reason is not None, missing_reason=reason,
                                  required=request.required_flags[name], defaulted=False,
                                  domain=domain, route=route, source_label=source["label"],
                                  metadata_release=self.metadata.release)
                if "window_days" in spec:
                    meta[name]["window_days"] = spec["window_days"]
        return FeatureResult(values, meta, [name for name in request.feature_names if name not in values],
                             version, ttl, request.timeout_ms if timed_out else latency,
                             "TIMEOUT" if timed_out else "OK")


class FeatureSession:
    """Collect gateway calls and their point-in-time feature snapshot for one request."""

    def __init__(self, service, request, feature_set_id, trace, fault):
        self.service = service
        self.request = request
        self.feature_set_id = feature_set_id
        self.trace = trace
        self.fault = fault
        self.config = service.metadata.definitions(feature_set_id)
        self.snapshot = FeatureResult({}, {}, [], self.config["version"], self.config["ttl"], 0, "OK")
        self.reasons = []

    def fetch(self, names, *, consumer, retry=False):
        specs = self.config["features"]
        request = self.request
        feature_request = FeatureRequest(
            request.trace_id, request.scene, request.entity_keys, self.feature_set_id, tuple(names),
            {name: specs[name]["critical"] for name in names}, request.as_of, self.trace.remaining,
            {name: request.business_context[name] for name in names
             if specs[name]["source"] == "request" and name in request.business_context},
        )
        result = self.service.batch_get(feature_request, self.fault, retry=retry)
        defaulted = []
        for name in result.missing_features:
            if not specs[name]["critical"] and result.status != "TIMEOUT":
                result.feature_values[name] = specs[name]["default"]
                result.feature_meta[name].update(defaulted=True, default_source="feature_config")
                defaulted.append(name)
        if defaulted:
            self.reasons.append("FEATURE_MISSING")
        routes = {meta["domain"]: meta["route"] for meta in result.feature_meta.values()}
        self.trace.add("feature.retry" if retry else "feature.batch_get", result.latency_ms,
                       consumer=consumer, status=result.status, names=list(names),
                       missing_features=result.missing_features, defaulted=defaulted, routes=routes,
                       feature_version=result.feature_version, metadata_release=self.service.metadata.release)
        snapshot = self.snapshot
        for name in names:
            snapshot.feature_values.pop(name, None)
        snapshot.feature_values.update(result.feature_values)
        snapshot.feature_meta.update(result.feature_meta)
        snapshot.missing_features = [name for name, entry in snapshot.feature_meta.items() if entry["missing"]]
        snapshot.feature_version = result.feature_version
        snapshot.latency_ms += result.latency_ms
        snapshot.status = result.status
        return result
