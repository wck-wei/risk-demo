from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from risk_demo.configuration import CONFIG_DIR, Configuration
from risk_demo.contracts import Fault, ModelRequest
from risk_demo.risk import RiskService

ROOT = Path(__file__).resolve().parents[1]


def sample(name, **changes):
    request = json.loads((ROOT / "examples" / f"{name}.json").read_text(encoding="utf-8"))
    request.update(changes)
    return request


def components(response):
    return response["component_results"]


def events(response, name=None):
    trace = components(response)["component_trace"]
    return [event for event in trace if event["event"] == name] if name else trace


class RiskDemoTests(unittest.TestCase):
    def setUp(self):
        self.service = RiskService()

    def test_t01_credit_happy_path(self):
        result = self.service.decide(sample("credit"))
        self.assertEqual(result["code"], "OK")
        self.assertEqual(result["decision"], "PASS")
        self.assertEqual(result["approved_amount"], 30000)
        self.assertEqual(result["approved_term"], 12)
        self.assertEqual(result["risk_level"], "LOW")
        self.assertEqual(result["reason_codes"], [])
        self.assertEqual(components(result)["model"]["score"], 0.82)

    def test_t02_order_happy_path(self):
        result = self.service.decide(sample("order"))
        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(result["approved_amount"], 4999)
        self.assertIs(result["need_verify"], False)
        self.assertEqual(result["risk_level"], "LOW")
        self.assertEqual(components(result)["model"]["probability"], 0.08)

    def test_t03_pricing_happy_path(self):
        result = self.service.decide(sample("pricing"))
        self.assertEqual(result["decision"], "PRICE_OK")
        self.assertEqual(result["final_rate"], 0.15)
        self.assertEqual(result["apr"], 0.162)
        self.assertEqual(result["risk_level"], "LOW")
        self.assertEqual(result["reason_codes"], ["LOW_RISK_PREMIUM"])
        self.assertEqual(components(result)["model"]["score"], 0.78)

    def test_t04_risky_customers(self):
        credit = self.service.decide(sample("credit", customer_id="CUST2002", device_id="DEV-B"))
        self.assertEqual(credit["decision"], "REJECT")
        self.assertEqual(credit["reason_codes"], ["CREDIT_OVERDUE", "CREDIT_MULTI_LOAN"])
        self.assertEqual(len(components(credit)["engine"]["hit_rules"]), 2)
        order = self.service.decide(sample("order", customer_id="CUST2002", merchant_id="M200"))
        self.assertEqual(order["decision"], "REJECT")
        self.assertEqual(order["reason_codes"],
                         ["ORDER_INSUFFICIENT_CREDIT", "ORDER_MERCHANT_RISK", "ORDER_SEVEN_DAY_LIMIT"])
        for result in (credit, order):
            self.assertIsNone(components(result)["model"])
            self.assertIsNone(result["approved_amount"])
            self.assertEqual(events(result, "model.predict"), [])

    def test_t05_configurable_model_thresholds(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            shutil.copytree(CONFIG_DIR, directory, dirs_exist_ok=True)
            model_file = directory / "models.json"
            models = deepcopy(self.service.config.models)
            models["CREDIT_SCORE_V1"]["threshold"] = 0.82
            models["ORDER_FRAUD_V1"]["threshold"] = 0.08
            model_file.write_text(json.dumps(models), encoding="utf-8")
            configured = RiskService(Configuration.load(directory))
            self.assertEqual(configured.decide(sample("credit"))["decision"], "PASS")
            order = configured.decide(sample("order"))
            self.assertEqual(order["decision"], "CHALLENGE")
            self.assertIs(order["need_verify"], True)
            self.assertEqual(order["reason_codes"], ["ORDER_MODEL_CHALLENGE"])
            models["CREDIT_SCORE_V1"]["threshold"] = 0.83
            model_file.write_text(json.dumps(models), encoding="utf-8")
            credit = RiskService(Configuration.load(directory)).decide(sample("credit"))
            self.assertEqual(credit["decision"], "REVIEW")
            self.assertEqual(credit["reason_codes"], ["CREDIT_SCORE_BELOW_THRESHOLD"])

    def test_t06_order_rule_boundaries(self):
        for amount, decision, reasons in [
            (13_800, "ALLOW", []),
            (13_800.01, "CHALLENGE", ["ORDER_SEVEN_DAY_LIMIT"]),
            (30_000, "CHALLENGE", ["ORDER_SEVEN_DAY_LIMIT"]),
            (30_000.01, "REJECT", ["ORDER_INSUFFICIENT_CREDIT", "ORDER_SEVEN_DAY_LIMIT"]),
        ]:
            with self.subTest(amount=amount):
                result = self.service.decide(sample("order", order_amount=amount, available_credit=90_000))
                self.assertEqual(result["decision"], decision)
                self.assertEqual(result["reason_codes"], reasons)
                self.assertEqual(components(result)["features"]["feature_values"]["available_credit"], 30_000)

    def test_t07_pricing_bounds_and_high_risk(self):
        for base, rate, apr in [(0, 0.08, 0.092), (0.23, 0.24, 0.252)]:
            with self.subTest(base_rate=base):
                result = self.service.decide(sample("pricing", base_rate=base))
                self.assertEqual((result["final_rate"], result["apr"]), (rate, apr))
        high = self.service.decide(sample("pricing", customer_id="CUST2002"))
        self.assertEqual((high["decision"], high["risk_level"], high["final_rate"]),
                         ("PRICE_OK", "HIGH", 0.2))
        self.assertEqual(high["reason_codes"], ["HIGH_RISK_PREMIUM"])
        matched = self.service.decide(sample("pricing", competitor_rate=0.145))
        self.assertEqual((matched["final_rate"], matched["apr"]), (0.145, 0.157))
        self.assertEqual(matched["reason_codes"], ["LOW_RISK_PREMIUM", "COMPETITOR_RATE_MATCHED"])
        high = self.service.decide(sample("pricing", customer_id="CUST2002", competitor_rate=0.197))
        self.assertEqual(high["final_rate"], 0.2)

    def test_t08_invalid_requests_never_reach_engine(self):
        requests = [None, [], sample("credit", scene="UNKNOWN"), sample("order", merchant_id=""),
                    sample("credit", amount=1), sample("credit", term_months=True),
                    sample("credit", apply_amount=float("inf")), sample("credit", apply_amount=10**400),
                    sample("order", available_credit=-1), sample("pricing", base_rate=1.2),
                    sample("pricing", timeout_ms=0), sample("credit", request_time="0001-01-01"),
                    sample("credit", request_time="2026-06-40")]
        missing = sample("pricing")
        del missing["base_rate"]
        requests.append(missing)
        for index, request in enumerate(requests):
            with self.subTest(input_index=index):
                result = self.service.decide(request)
                self.assertEqual((result["code"], result["decision"]), ("PARAM_INVALID", "INVALID"))
                self.assertEqual(result["reason_codes"], ["PARAM_INVALID"])
                self.assertEqual([event["event"] for event in events(result)], ["risk.received", "risk.response"])
                self.assertTrue(result["trace_id"])
                self.assertTrue(all(event["trace_id"] == result["trace_id"] for event in events(result)))
                self.assertIsNone(components(result)["engine"])

    def test_t09_chain_contract_and_audit(self):
        engine = self.service.engine
        with patch.object(engine.feature_service, "batch_get", wraps=engine.feature_service.batch_get) as features:
            with patch.object(self.service.predictor, "predict", wraps=self.service.predictor.predict) as model:
                result = self.service.decide(sample("credit", trace_id="TRACE-T09", age=90))
        self.assertEqual(features.call_count, 2)
        model.assert_called_once()
        feature_request, model_feature_request = [call.args[0] for call in features.call_args_list]
        self.assertNotIn("income_level", feature_request.feature_names)
        self.assertIn("income_level", model_feature_request.feature_names)
        self.assertEqual(model_feature_request.as_of, feature_request.as_of)
        model_request = model.call_args.args[0]
        self.assertEqual(feature_request.trace_id, model_request.trace_id)
        self.assertEqual(model_request.trace_id, "TRACE-T09")
        self.assertEqual(set(feature_request.entity_keys), {"customer_id", "apply_id", "device_id"})
        self.assertEqual(feature_request.request_features, {})
        self.assertEqual(set(model_request.feature_vector), {"cust_risk_prob", "income_level", "device_risk_score"})
        self.assertEqual(model_request.feature_vector.keys(), model_request.feature_meta.keys())
        self.assertTrue({"id_hash", "phone_hash", "age"}.isdisjoint(model_request.business_context))
        self.assertEqual(components(result)["features"]["feature_values"]["age"], 32)
        self.assertEqual([event["event"] for event in events(result)],
                         ["risk.received", "rcms.route", "engine.strategy", "feature.batch_get", "engine.rules",
                          "engine.rules", "engine.rules", "model.gateway", "feature.batch_get",
                          "model.predict", "engine.decision", "risk.response"])
        self.assertEqual([event["sequence"] for event in events(result)], list(range(1, 13)))
        self.assertEqual({event["trace_id"] for event in events(result)}, {"TRACE-T09"})
        self.assertEqual(events(result)[-1]["elapsed_ms"], 18)
        self.assertEqual(model_request.timeout_ms, 90)
        self.assertEqual(feature_request.as_of.isoformat(), "2026-06-01T10:00:00+00:00")

    def test_t10_missing_model_feature_recovers(self):
        result = self.service.decide(sample("credit"), fault=Fault("missing-once", "income_level"))
        self.assertEqual(result["decision"], "PASS")
        self.assertEqual(result["reason_codes"], ["MODEL_MISSING_FEATURE"])
        self.assertEqual([event["names"] for event in events(result, "feature.retry")], [["income_level"]])
        self.assertEqual([event["status"] for event in events(result, "model.predict")], ["MISSING_FEATURES", "OK"])
        snapshot = components(result)["features"]
        self.assertEqual(snapshot["feature_values"]["income_level"], 4)
        self.assertEqual(snapshot["missing_features"], [])
        self.assertEqual(snapshot["latency_ms"], 15)
        self.assertEqual(events(result)[-1]["elapsed_ms"], 31)

    def test_t11_missing_model_feature_persists(self):
        result = self.service.decide(sample("credit"), fault=Fault("missing-always", "income_level"))
        self.assertEqual(result["decision"], "REVIEW")
        self.assertEqual(result["reason_codes"], ["MODEL_MISSING_FEATURE"])
        self.assertIsNone(result["approved_amount"])
        self.assertEqual(len(events(result, "feature.retry")), 1)
        self.assertEqual(len(events(result, "model.predict")), 2)
        self.assertEqual(components(result)["model"]["missing_features"], ["income_level"])

    def test_t12_defaults_and_stale_required_features(self):
        for fault, reason in [("missing-always", "NOT_FOUND"), ("stale", "STALE")]:
            with self.subTest(fault=fault):
                result = self.service.decide(sample("order"), fault=Fault(fault, "repay_capacity_score"))
                self.assertEqual(result["decision"], "ALLOW")
                self.assertEqual(result["reason_codes"], ["FEATURE_MISSING"])
                snapshot = components(result)["features"]
                self.assertEqual(snapshot["feature_values"]["repay_capacity_score"], 0.5)
                self.assertIn("repay_capacity_score", snapshot["missing_features"])
                meta = snapshot["feature_meta"]["repay_capacity_score"]
                self.assertEqual(meta["missing_reason"], reason)
                self.assertIs(meta["defaulted"], True)
                self.assertEqual(meta["default_source"], "feature_config")
        required = self.service.decide(sample("credit"), fault=Fault("stale", "age"))
        self.assertEqual(required["decision"], "REVIEW")
        self.assertEqual(required["reason_codes"], ["FEATURE_MISSING"])
        self.assertEqual(events(required, "model.predict"), [])

    def test_t13_feature_and_retry_timeouts(self):
        result = self.service.decide(sample("credit", timeout_ms=4))
        self.assertEqual((result["decision"], result["reason_codes"]), ("REVIEW", ["FEATURE_TIMEOUT_FALLBACK"]))
        self.assertEqual(events(result)[-1]["elapsed_ms"], 4)
        forced = self.service.decide(sample("order"), fault=Fault("feature-timeout"))
        self.assertEqual(forced["decision"], "CHALLENGE")
        self.assertEqual(components(forced)["features"]["status"], "TIMEOUT")
        retry = self.service.decide(sample("credit", timeout_ms=19), fault=Fault("missing-once", "income_level"))
        self.assertEqual(retry["decision"], "REVIEW")
        self.assertEqual(retry["reason_codes"], ["MODEL_MISSING_FEATURE", "FEATURE_TIMEOUT_FALLBACK"])
        self.assertEqual(events(retry, "feature.retry")[0]["latency_ms"], 1)
        self.assertEqual(events(retry)[-1]["elapsed_ms"], 19)

    def test_t14_model_timeout_by_scene(self):
        for name, decision, reason in [
            ("credit", "REVIEW", "MODEL_TIMEOUT_REVIEW"),
            ("order", "CHALLENGE", "MODEL_TIMEOUT_RULE_ONLY"),
            ("pricing", "REVIEW", "MODEL_TIMEOUT_REVIEW"),
        ]:
            with self.subTest(scene=name):
                result = self.service.decide(sample(name, timeout_ms=11))
                self.assertEqual((result["decision"], result["reason_codes"]), (decision, [reason]))
                self.assertEqual(components(result)["model"]["status"], "TIMEOUT")
                self.assertIsNone(result["approved_amount"])
                self.assertIsNone(result["final_rate"])
                self.assertEqual(events(result, "model.predict")[0]["latency_ms"], 6)
                self.assertEqual(events(result)[-1]["elapsed_ms"], 11)
                self.assertEqual(result["need_verify"], True if name == "order" else None)

    def test_t15_model_checks_types_and_versions(self):
        result = self.service.decide(sample("credit", customer_id="CUST2002", device_id="DEV-B"),
                                     fault=Fault("version"))
        self.assertEqual(result["decision"], "REVIEW")
        self.assertEqual(result["reason_codes"], ["FEATURE_VERSION_MISMATCH"])
        self.assertIsNone(components(result)["model"])
        self.assertEqual(components(result)["features"]["status"], "FEATURE_VERSION_MISMATCH")
        self.assertEqual(events(result, "engine.rules"), [])
        request = ModelRequest("T15", "CREDIT_APPLY", "CREDIT_SCORE_V1", "1.0", "FS_V1.0",
                               {"income_level": "4", "cust_risk_prob": 0.12, "device_risk_score": 0.1},
                               {name: {"version": "FS_V1.0"} for name in
                                ("income_level", "cust_risk_prob", "device_risk_score")},
                               {}, {"threshold": 0.65}, 10)
        model = self.service.predictor
        self.assertEqual(model.predict(request, Fault()).status, "INVALID_FEATURE_TYPE")
        wrong_meta = {**request.feature_meta, "income_level": {"version": "FS_WRONG"}}
        self.assertEqual(model.predict(replace(request, feature_meta=wrong_meta), Fault()).status,
                         "FEATURE_VERSION_MISMATCH")
        self.assertEqual(model.predict(replace(request, model_version="0"), Fault()).status,
                         "MODEL_VERSION_MISMATCH")

    def test_t16_invalid_configuration_fails_at_load(self):
        for name, key, modified, message in [
            ("scenes", "CREDIT_APPLY", {"feature_set_id": "UNKNOWN"}, "UNKNOWN"),
            ("rules", "CREDIT_STD_V1", [{"op": "exec"}], "不支持规则操作"),
            ("models", "CREDIT_SCORE_V1", {"weights": {"unknown": 0.1}}, "未知特征"),
            ("scenes", "CREDIT_APPLY", {"fallback": "ALLOW"}, "无效降级动作"),
        ]:
            with self.subTest(file=name), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                shutil.copytree(CONFIG_DIR, directory, dirs_exist_ok=True)
                configuration = deepcopy(getattr(self.service.config, name))
                if isinstance(modified, dict):
                    configuration[key].update(modified)
                else:
                    configuration[key] = modified
                (directory / f"{name}.json").write_text(json.dumps(configuration), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    Configuration.load(directory)

    def test_t17_cli_json_audit_exit_codes_and_fault(self):
        def call(*args, stdin=None):
            return subprocess.run([sys.executable, "-m", "risk_demo", *args], cwd=ROOT,
                                  input=stdin, text=True, encoding="utf-8", capture_output=True, timeout=10)

        for name, decision in (("credit", "PASS"), ("order", "ALLOW"), ("pricing", "PRICE_OK")):
            with self.subTest(scene=name):
                process = call(f"examples/{name}.json", "--audit")
                self.assertEqual(process.returncode, 0, process.stderr)
                response = json.loads(process.stdout)
                self.assertEqual(response["decision"], decision)
                self.assertEqual([json.loads(line) for line in process.stderr.splitlines()], events(response))
        process = call("-", "--audit", stdin="{ malformed")
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stdout)["code"], "PARAM_INVALID")
        self.assertEqual(len(process.stderr.splitlines()), 2)
        process = call("-", "--fault", "missing-once", "--feature", "income_level",
                       stdin=json.dumps(sample("credit")))
        self.assertEqual(process.returncode, 0)
        self.assertEqual(json.loads(process.stdout)["reason_codes"], ["MODEL_MISSING_FEATURE"])
        self.assertEqual(process.stderr, "")
        process = call("examples/credit.json", "--fault", "missing-once", "--feature", "competitor_rate")
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stdout)["code"], "PARAM_INVALID")

    def test_t18_parallel_requests_are_isolated(self):
        cases = [(sample("credit", trace_id=f"T18-{index}"),
                  Fault("missing-always", "income_level") if index % 2 else Fault())
                 for index in range(20)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(self.service.decide, request, fault=fault) for request, fault in cases]
        for index, future in enumerate(futures):
            result = future.result()
            self.assertEqual({event["trace_id"] for event in events(result)}, {f"T18-{index}"})
            self.assertEqual(result["decision"], "REVIEW" if index % 2 else "PASS")
            self.assertEqual(events(result)[-1]["elapsed_ms"], 31 if index % 2 else 18)
            self.assertEqual(len(events(result, "feature.retry")), index % 2)

    def test_t19_unknown_entities_and_request_sourced_features(self):
        result = self.service.decide(sample("credit", customer_id="UNKNOWN"))
        self.assertEqual(result["decision"], "REVIEW")
        self.assertIsNone(result["approved_amount"])
        self.assertEqual(components(result)["features"]["feature_meta"]["age"]["missing_reason"], "NOT_FOUND")
        present = self.service.decide(sample("pricing", competitor_rate=0.13, term_months=24))
        values = components(present)["features"]["feature_values"]
        self.assertEqual((values["competitor_rate"], values["term_months"]), (0.13, 24))
        no_competitor = sample("pricing")
        del no_competitor["competitor_rate"]
        result = self.service.decide(no_competitor)
        self.assertEqual(components(result)["features"]["feature_values"]["competitor_rate"], 0.14)
        self.assertEqual(result["reason_codes"], ["FEATURE_MISSING", "LOW_RISK_PREMIUM"])


if __name__ == "__main__":
    unittest.main()
