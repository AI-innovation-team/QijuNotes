"""No real provider, credentials or production database are used by these tests."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location("verify_live_model", Path(__file__).resolve().parents[1] / "scripts/verify-live-model.py")
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class LiveValidationHarnessTests(unittest.TestCase):
    def test_plan_does_not_read_config_auth_or_start_application(self):
        output = io.StringIO()
        with patch.object(Path, "read_text", side_effect=AssertionError("no credential reads")), patch.object(live, "Application", side_effect=AssertionError("no instance")), contextlib.redirect_stdout(output):
            self.assertEqual(live.main(["--phase", "full"]), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual((plan["minimum_provider_requests"], plan["maximum_provider_requests"], plan["output_limit"]), (15, 24, 1200))
        self.assertFalse(plan["human_acceptance"])

    def test_atomic_global_and_per_turn_limits(self):
        budget = live.CallBudget(5, 3)
        budget.begin("first")
        def claim():
            try:
                budget.claim()
            except live.LimitReached:
                pass
        threads = [threading.Thread(target=claim) for _ in range(20)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(budget.records), 3)
        budget.begin("second")
        claim(); claim(); claim()
        self.assertEqual(len(budget.records), 5)
        self.assertTrue(budget.rejections)

    def test_relay_caps_output_counts_requests_and_rejects_before_upstream(self):
        captured = []
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "application/json"}
            def __init__(self):
                super().__init__(b'{"choices":[{"message":{"content":"synthetic answer"}}],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}')
        class Deadline:
            def __init__(self, seconds): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def before_read(self): pass
            def open(self, request):
                captured.append({"body": json.loads(request.data), "authorization": request.get_header("Authorization")})
                return Response()
        relay = live.GatewayRelay("/not-a-real-data-directory", "synthetic-internal-key", live.CallBudget(2, 2), 1200, 60)
        with patch.object(live, "RequestDeadline", Deadline), patch.object(live, "usage_rows", return_value={}):
            url = relay.start()
            try:
                for _ in range(2):
                    request = urllib.request.Request(url + "/chat/completions", b'{"model":"foreground-model","max_tokens":8192,"messages":[]}', {"Authorization": "Bearer " + relay.local_key, "Content-Type": "application/json"})
                    with urllib.request.urlopen(request) as response:
                        self.assertEqual(json.load(response)["usage"]["total_tokens"], 15)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 429)
                self.assertEqual(len(captured), 2)
                self.assertTrue(all(row["body"]["max_tokens"] == 1200 for row in captured))
                self.assertTrue(all(row["authorization"] == "Bearer synthetic-internal-key" for row in captured))
                self.assertTrue(all(row["reported_total"] == 15 for row in relay.budget.records))
                self.assertNotIn("synthetic-internal-key", json.dumps(relay.budget.records))
            finally:
                relay.close()


if __name__ == "__main__":
    unittest.main()
