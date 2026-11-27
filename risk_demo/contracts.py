from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from typing import Any


def matches_type(value: Any, kind: str) -> bool:
    if kind == "integer":
        return type(value) is int
    if kind == "number" and type(value) in (int, float):
        try:
            return isfinite(value)
        except OverflowError:
            return False
    return False


@dataclass(frozen=True)
class EngineRequest:
    trace_id: str
    scene: str
    strategy_id: str
    business_context: dict
    entity_keys: dict[str, str]
    as_of: datetime
    timeout_ms: int


@dataclass(frozen=True)
class FeatureRequest:
    trace_id: str
    scene: str
    entity_keys: dict[str, str]
    feature_set_id: str
    feature_names: tuple[str, ...]
    required_flags: dict[str, bool]
    as_of: datetime
    timeout_ms: int
    request_features: dict


@dataclass
class FeatureResult:
    feature_values: dict
    feature_meta: dict
    missing_features: list[str]
    feature_version: str
    ttl: int
    latency_ms: int
    status: str


@dataclass(frozen=True)
class ModelRequest:
    trace_id: str
    scene: str
    model_code: str
    model_version: str
    feature_version_used: str
    feature_vector: dict
    feature_meta: dict
    business_context: dict
    threshold_profile: dict[str, float]
    timeout_ms: int


@dataclass
class ModelResult:
    status: str
    threshold: float
    model_version: str
    feature_version_used: str
    latency_ms: int
    score: float | None = None
    probability: float | None = None
    risk_level: str | None = None
    decision_hint: str | None = None
    missing_features: list[str] = field(default_factory=list)
    risk_premium: float | None = None
    suggested_rate: float | None = None


@dataclass
class EngineResult:
    decision: str
    reason_codes: list[str]
    hit_rules: list[dict]
    model_result: ModelResult | None
    feature_snapshot: FeatureResult
    component_trace: list[dict]
    next_action: str
    output: dict


@dataclass(frozen=True)
class Fault:
    kind: str = "none"
    feature: str | None = None


@dataclass
class Trace:
    trace_id: str
    timeout_ms: int
    events: list[dict] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def remaining(self):
        return self.timeout_ms - self.elapsed_ms

    def add(self, event, latency_ms=0, **details):
        self.elapsed_ms += latency_ms
        self.events.append(dict(trace_id=self.trace_id, sequence=len(self.events) + 1,
                                event=event, latency_ms=latency_ms,
                                elapsed_ms=self.elapsed_ms, **details))
