from copy import deepcopy
from dataclasses import dataclass, replace
from threading import Lock

from .contracts import ModelRequest, ModelResult


@dataclass(frozen=True)
class RuleFlow:
    strategy_id: str
    version: str
    stages: tuple[tuple[str, tuple[dict, ...]], ...]
    engine_features: tuple[str, ...]


class RCMS:
    """Resolve the published rule flow for an online request."""

    def __init__(self, config):
        self._flows = {}
        for name, scene in config.scenes.items():
            strategy = scene["strategy_id"]
            rules = config.rules[strategy]
            stages = tuple((stage, tuple(deepcopy(rule) for rule in rules if rule["stage"] == stage))
                           for stage in scene["rule_stages"])
            self._flows[name] = RuleFlow(strategy, scene["flow_version"], stages,
                                         tuple(scene["engine_features"]))

    def flow(self, scene):
        return deepcopy(self._flows[scene])


class MetadataPlatform:
    """Expose the published data catalog and its separately configured definitions."""

    def __init__(self, metadata, definitions):
        self.release = metadata["release"]
        self._sources = deepcopy(metadata["sources"])
        self._definitions = deepcopy(definitions)

    def definitions(self, feature_set_id):
        return deepcopy(self._definitions[feature_set_id])

    def source(self, source_id):
        return self._sources[source_id].copy()


class MagicCube:
    """In-process model registry and online prediction interface."""

    def __init__(self, config, predictor):
        self._config = config
        self._predictor = predictor
        self._lock = Lock()
        self._versions = {}
        self._active = {}
        for code, model in config.models.items():
            self._versions[code, model["version"]] = (deepcopy(model), "DEPLOYED")
            self._active[code] = model["version"]

    def _record(self, code, version):
        try:
            return self._versions[code, version]
        except KeyError as exc:
            raise ValueError("模型版本不存在") from exc

    def register(self, code, version, overrides=None):
        with self._lock:
            if code not in self._config.models or not isinstance(version, str) or not version.strip():
                raise ValueError("模型编码或版本无效")
            if (code, version) in self._versions:
                raise ValueError("模型版本已存在")
            definition = {**self._config.models[code], **(overrides or {}), "version": version}
            self._versions[code, version] = (deepcopy(definition), "REGISTERED")

    def validate(self, code, version):
        with self._lock:
            definition, state = self._record(code, version)
            if state not in {"REGISTERED", "VALIDATED"}:
                raise ValueError("只有已注册且未部署的模型可校验")
            models = {**self._config.models, code: definition}
            replace(self._config, models=models).validate()
            self._versions[code, version] = (definition, "VALIDATED")

    def deploy(self, code, version):
        with self._lock:
            definition, state = self._record(code, version)
            if self._active.get(code) == version:
                return
            if state != "VALIDATED":
                raise ValueError("模型必须先通过校验")
            previous = self._active.get(code)
            if previous is not None:
                previous_definition, _ = self._record(code, previous)
                self._versions[code, previous] = (previous_definition, "VALIDATED")
            self._versions[code, version] = (definition, "DEPLOYED")
            self._active[code] = version

    def undeploy(self, code):
        with self._lock:
            version = self._active.pop(code, None)
            if version is not None:
                definition, _ = self._record(code, version)
                self._versions[code, version] = (definition, "VALIDATED")

    def retire(self, code, version):
        with self._lock:
            definition, state = self._record(code, version)
            if state == "DEPLOYED":
                raise ValueError("先切换或撤回在用模型")
            self._versions[code, version] = (definition, "RETIRED")

    def versions(self, code):
        with self._lock:
            return {version: state for (model_code, version), (_, state) in self._versions.items()
                    if model_code == code}

    def predict(self, code, request, features, trace, fault):
        with self._lock:
            version = self._active.get(code)
            definition = self._record(code, version)[0] if version is not None else None
        trace.add("model.gateway", model_code=code, model_version=version, platform="Magic Cube")
        if definition is None:
            return self._record_prediction(trace, ModelResult("MODEL_UNAVAILABLE", None, None,
                                                              features.snapshot.feature_version, 0))
        if fault.kind == "model-timeout" or trace.remaining < definition["latency_ms"]:
            return self._record_prediction(trace, ModelResult("TIMEOUT", definition["threshold"], version,
                                                              features.snapshot.feature_version, trace.remaining))

        def infer(batch):
            if batch.status == "TIMEOUT":
                result = ModelResult("FEATURE_TIMEOUT", definition["threshold"], version,
                                     batch.feature_version, 0)
            else:
                inference = ModelRequest(
                    request.trace_id, request.scene, code, version, batch.feature_version, batch.feature_values,
                    batch.feature_meta, request.business_context, {"threshold": definition["threshold"]},
                    trace.remaining,
                )
                result = self._predictor.predict(inference, fault, model_config=definition)
            return self._record_prediction(trace, result)

        batch = features.fetch(tuple(definition["weights"]), consumer="magic_cube")
        result = infer(batch)
        if result.status == "MISSING_FEATURES":
            features.reasons.append("MODEL_MISSING_FEATURE")
            retry = features.fetch(result.missing_features, consumer="magic_cube", retry=True)
            if retry.status == "TIMEOUT" or retry.feature_version != batch.feature_version:
                status = "FEATURE_TIMEOUT" if retry.status == "TIMEOUT" else "FEATURE_VERSION_MISMATCH"
                return self._record_prediction(trace, ModelResult(status, definition["threshold"], version,
                                                                  retry.feature_version, 0))
            batch.feature_values.update(retry.feature_values)
            batch.feature_meta.update(retry.feature_meta)
            result = infer(batch)
        return result

    @staticmethod
    def _record_prediction(trace, result):
        trace.add("model.predict", result.latency_ms, status=result.status, model_version=result.model_version,
                  feature_version_used=result.feature_version_used, missing_features=result.missing_features,
                  decision_hint=result.decision_hint, score=result.score)
        return result
