from fastapi.testclient import TestClient

from cycleflow.api import create_app
from tests.common import ServiceCase


class IterationApiTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(str(self.path)))

    def _post(self, event):
        response = self.client.post("/events", json=event)
        self.assertEqual(response.json()["state"], "accepted", response.json())

    def test_iteration_endpoints_expose_windows_impact_and_trace(self):
        self._post({"source": "api", "external_id": "d1", "kind": "design.release",
                    "occurred_at": "2026-02-01T09:00:00Z",
                    "payload": {"design_id": "R1", "product": "arm", "revision": 1,
                                "parts": {"motor": 1}}})
        self._post({"source": "api", "external_id": "l1", "kind": "lot.receive",
                    "occurred_at": "2026-02-01T09:01:00Z",
                    "payload": {"lot_id": "L1", "part": "motor", "supplier": "acme",
                                "quantity": 4}})
        self._post({"source": "api", "external_id": "p1", "kind": "plan.create",
                    "occurred_at": "2026-02-01T09:02:00Z",
                    "payload": {"plan_id": "P1", "product": "arm", "revision": 1,
                                "minimum_pass_rate": "0.75"}})
        self._post({"source": "api", "external_id": "g1", "kind": "goal.define",
                    "occurred_at": "2026-02-01T09:03:00Z",
                    "payload": {"goal_id": "G1", "product": "arm", "title": "target",
                                "minimum_pass_rate": "0.75", "plan_id": "P1"}})
        self._post({"source": "api", "external_id": "w1", "kind": "window.define",
                    "occurred_at": "2026-02-01T09:04:00Z",
                    "payload": {"window_id": "W1", "product": "arm",
                                "window_start": "2026-02-02T00:00:00Z",
                                "window_end": "2026-02-09T00:00:00Z", "goal_id": "G1"}})
        weekly = self.client.get("/iterations/arm/weekly").json()
        self.assertEqual(weekly["attribution"].startswith("job.opened_at"), True)
        self.assertEqual(weekly["windows"][0]["window_id"], "W1")
        self.assertIn("trace", weekly["windows"][0])
        listing = self.client.get("/iterations/arm/windows").json()
        self.assertEqual([w["window_id"] for w in listing["windows"]], ["W1"])
        goal = self.client.get("/iterations/goals/G1").json()
        self.assertEqual(goal["version"], 1)
        drift = self.client.get("/iterations/arm/goal-drift").json()
        self.assertEqual(drift["windows"][0]["calibration_stale"], False)
        # Redefine and read impact over HTTP.
        self._post({"source": "api", "external_id": "w1v2", "kind": "window.redefine",
                    "occurred_at": "2026-02-05T09:00:00Z",
                    "payload": {"window_id": "W1",
                                "window_start": "2026-02-02T00:00:00Z",
                                "window_end": "2026-02-08T00:00:00Z",
                                "reason": "shift"}})
        impact = self.client.get(
            "/iterations/windows/W1/impact", params={"from_version": 1}).json()
        self.assertEqual(impact["to_version"], 2)
        self.assertEqual(impact["moved_in"], [])
