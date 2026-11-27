from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import patch

from risk_demo.contracts import Fault
from risk_demo.risk import RiskService

from test_risk_demo import components, events, sample


class PlatformTests(TestCase):
    def setUp(self):
        self.service = RiskService()

    def test_p01_published_flow_and_policy_branches(self):
        for name in ("credit", "order", "pricing"):
            with self.subTest(scene=name):
                result = self.service.decide(sample(name, customer_id="CUST3003"))
                self.assertEqual((result["decision"], result["reason_codes"]), ("REVIEW", ["AML_MANUAL_REVIEW"]))
                self.assertEqual(events(result, "rcms.route")[0]["flow_version"], "RCMS_1.0")
                self.assertEqual([item["stage"] for item in events(result, "engine.rules")], ["AML"])
                self.assertFalse(events(result, "model.gateway"))
        fraud = self.service.decide(sample("order", device_id="DEV-B"))
        self.assertEqual((fraud["decision"], fraud["reason_codes"]), ("CHALLENGE", ["FRAUD_DEVICE_VERIFY"]))
        self.assertTrue(fraud["need_verify"])
        self.assertFalse(events(fraud, "model.gateway"))

    def test_p01_rule_flow_reconfiguration_and_validation(self):
        scenes = deepcopy(self.service.config.scenes)
        scenes["ORDER_LOAN"].update(flow_version="RCMS_1.1", rule_stages=["AML", "FRAUD", "BUSINESS"])
        config = replace(self.service.config, scenes=scenes)
        config.validate()
        result = RiskService(config).decide(sample("order", device_id="DEV-B", order_amount=31000))
        self.assertEqual(result["decision"], "CHALLENGE")
        self.assertEqual(events(result, "rcms.route")[0]["flow_version"], "RCMS_1.1")
        self.assertEqual([item["stage"] for item in events(result, "engine.rules")], ["AML", "FRAUD"])
        scenes["ORDER_LOAN"]["rule_stages"].append("FRAUD")
        with self.assertRaisesRegex(ValueError, "阶段无效或重复"):
            config.validate()

    def test_p02_metadata_routing_and_order_report_window(self):
        request = sample("order")
        as_of = datetime.fromisoformat(request["request_time"])
        service = self.service.feature_service
        self.assertEqual(self.service.metadata.source("order_events")["route"], "OUTER_API")
        original = self.service.decide(request)
        self.assertEqual(components(original)["features"]["feature_values"]["order_amt_7d"], 6200)
        for key, delta, amount in [
            ("included", -7, 9000), ("future", 1, 20000), ("out-of-window", -8, 20000),
            ("at-request-time", 0, 20000), ("included", -7, 9000),
        ]:
            service.report_order("CUST1001", key, amount, as_of + timedelta(days=delta))
        with self.assertRaisesRegex(ValueError, "内容不一致"):
            service.report_order("CUST1001", "included", 5, as_of - timedelta(days=7))
        with self.assertRaisesRegex(ValueError, "时区"):
            service.report_order("CUST1001", "naive", 5, as_of.replace(tzinfo=None))
        result = self.service.decide(request)
        features = components(result)["features"]
        self.assertEqual(features["feature_values"]["order_amt_7d"], 15200)
        self.assertEqual(features["feature_meta"]["order_amt_7d"]["window_days"], 7)
        self.assertEqual(features["feature_meta"]["order_amt_7d"]["metadata_release"], "META_1.0")
        self.assertEqual(set(events(result, "feature.batch_get")[0]["routes"].values()),
                         {"LOCAL", "TABLE", "OUTER_API"})
        self.assertEqual((result["decision"], result["reason_codes"]), ("CHALLENGE", ["ORDER_SEVEN_DAY_LIMIT"]))

    def test_p03_retry_only_for_magic_cube_consumer(self):
        result = self.service.decide(sample("credit"), fault=Fault("missing-once", "income_level"))
        batches = events(result, "feature.batch_get")
        self.assertEqual([event["consumer"] for event in batches], ["engine", "magic_cube"])
        self.assertNotIn("income_level", batches[0]["names"])
        retries = events(result, "feature.retry")
        self.assertEqual([(event["consumer"], event["names"]) for event in retries], [("magic_cube", ["income_level"])])
        self.assertEqual(result["decision"], "PASS")
        missing = self.service.decide(sample("order"), fault=Fault("missing-always", "watchlist_for_review"))
        self.assertEqual((missing["decision"], missing["reason_codes"]), ("CHALLENGE", ["FEATURE_MISSING"]))
        self.assertFalse(events(missing, "model.gateway"))

    def test_p04_magic_cube_lifecycle_changes_online_version(self):
        cube = self.service.magic_cube
        code = "CREDIT_SCORE_V1"
        request = sample("credit")
        cube.register(code, "2.0", {"threshold": 0.84})
        self.assertEqual(cube.versions(code), {"1.0": "DEPLOYED", "2.0": "REGISTERED"})
        with self.assertRaisesRegex(ValueError, "先通过校验"):
            cube.deploy(code, "2.0")
        cube.validate(code, "2.0")
        cube.deploy(code, "2.0")
        result = self.service.decide(request)
        self.assertEqual((result["decision"], components(result)["model"]["model_version"]), ("REVIEW", "2.0"))
        with self.assertRaisesRegex(ValueError, "在用模型"):
            cube.retire(code, "2.0")
        cube.deploy(code, "1.0")
        cube.retire(code, "2.0")
        self.assertEqual(self.service.decide(request)["decision"], "PASS")
        with self.assertRaisesRegex(ValueError, "先通过校验"):
            cube.deploy(code, "2.0")
        cube.undeploy(code)
        unavailable = self.service.decide(request)
        self.assertEqual((unavailable["decision"], unavailable["reason_codes"]), ("REVIEW", ["MODEL_UNAVAILABLE"]))
        self.assertEqual(len(events(unavailable, "feature.batch_get")), 1)
        self.assertIsNone(events(unavailable, "model.gateway")[0]["model_version"])

    def test_p04_release_is_pinned_while_fetching_model_features(self):
        cube = self.service.magic_cube
        code = "CREDIT_SCORE_V1"
        cube.register(code, "2.0", {"threshold": 0.84})
        cube.validate(code, "2.0")
        feature_service = self.service.feature_service
        batch_get = feature_service.batch_get

        def deploy_during_fetch(request, fault, *, retry=False):
            if "income_level" in request.feature_names:
                cube.deploy(code, "2.0")
            return batch_get(request, fault, retry=retry)

        with patch.object(feature_service, "batch_get", side_effect=deploy_during_fetch):
            result = self.service.decide(sample("credit"))
        self.assertEqual((result["decision"], components(result)["model"]["model_version"]), ("PASS", "1.0"))
        self.assertEqual(self.service.decide(sample("credit"))["decision"], "REVIEW")
