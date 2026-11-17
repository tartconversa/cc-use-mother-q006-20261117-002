from cycleflow.contracts import DomainError
from tests.common import ServiceCase


def ts(day, hour=9, minute=0):
    return f"2026-01-{day:02d}T{hour:02d}:{minute:02d}:00Z"


def midnight(day):
    return f"2026-01-{day:02d}T00:00:00Z"


def norm(value):
    return value.replace("Z", "+00:00")


class IterationGovernanceTests(ServiceCase):
    def setUp(self):
        super().setUp()
        # Two adjacent iteration windows, Monday-Sunday boundaries chosen by the team.
        def ev(external, kind, day, payload, hour=9, minute=0):
            return self.service.ingest({"source": "test", "external_id": external, "kind": kind,
                                        "occurred_at": ts(day, hour, minute), "payload": payload})
        ev("d1", "design.release", 1, {"design_id": "R1", "product": "robot-arm",
                                       "revision": 1, "parts": {"motor": 2, "sensor": 1}})
        ev("l1", "lot.receive", 1, {"lot_id": "M1", "part": "motor",
                                    "supplier": "motors", "quantity": 40})
        ev("l2", "lot.receive", 1, {"lot_id": "S1", "part": "sensor",
                                    "supplier": "sensors", "quantity": 20})
        ev("p1", "plan.create", 1, {"plan_id": "P1", "product": "robot-arm",
                                    "revision": 1, "minimum_pass_rate": "0.75"})
        self.goal = ev("g1", "goal.define", 1, {
            "goal_id": "G1", "product": "robot-arm", "title": "weekly arm target",
            "minimum_pass_rate": "0.75", "plan_id": "P1"})
        self.w1 = ev("w1", "window.define", 1, {
            "window_id": "W1", "product": "robot-arm",
            "window_start": midnight(5), "window_end": midnight(12), "goal_id": "G1"})
        self.w2 = ev("w2", "window.define", 1, {
            "window_id": "W2", "product": "robot-arm",
            "window_start": midnight(12), "window_end": midnight(19), "goal_id": "G1"})

    def _open(self, external, job_id, day, parent=None, quantity=4):
        payload = {"job_id": job_id, "design_id": "R1", "quantity": quantity}
        if parent:
            payload["parent_job"] = parent
        result = self.service.ingest({"source": "test", "external_id": external,
                                      "kind": "job.open", "occurred_at": ts(day, 9),
                                      "payload": payload})
        self.assertEqual(result["state"], "accepted", result)

    def _stage(self, external, job_id, stage, day, hour):
        result = self.service.ingest({
            "source": "test", "external_id": external, "kind": "job.stage_start",
            "occurred_at": ts(day, hour), "payload": {"job_id": job_id, "stage": stage}})
        self.assertEqual(result["state"], "accepted", result)
        result = self.service.ingest({
            "source": "test", "external_id": external + "-f", "kind": "job.stage_finish",
            "occurred_at": ts(day, hour + 1), "payload": {"job_id": job_id, "stage": stage}})
        self.assertEqual(result["state"], "accepted", result)

    def _test(self, external, test_id, job_id, day, passed=3, failed=1):
        result = self.service.ingest({
            "source": "test", "external_id": external, "kind": "test.record",
            "occurred_at": ts(day, 15),
            "payload": {"test_id": test_id, "job_id": job_id, "plan_id": "P1",
                        "passed": passed, "failed": failed}})
        self.assertEqual(result["state"], "accepted", result)

    # ----- versioning and immutability ------------------------------------
    def test_goal_and_window_are_versioned_append_only(self):
        self.assertEqual(self.service.get_goal("G1")["version"], 1)
        self.assertEqual(self.service.get_window("W1")["current_version"], 1)
        revised = self.service.ingest({
            "source": "test", "external_id": "g2", "kind": "goal.define",
            "occurred_at": ts(6),
            "payload": {"goal_id": "G1", "product": "robot-arm", "title": "stricter target",
                        "minimum_pass_rate": "0.9", "reason": "tighten after review"}})
        self.assertEqual(revised["state"], "accepted", revised)
        self.assertEqual(self.service.get_goal("G1")["version"], 2)
        original = self.service.get_goal("G1", 1)
        self.assertEqual(original["minimum_pass_rate"], "0.75")
        self.assertEqual(self.service.get_goal("G1")["minimum_pass_rate"], "0.9")
        duplicate = self.service.ingest({
            "source": "test", "external_id": "wdup", "kind": "window.define",
            "occurred_at": ts(1),
            "payload": {"window_id": "W1", "product": "robot-arm",
                        "window_start": midnight(5), "window_end": midnight(12), "goal_id": "G1"}})
        self.assertEqual(duplicate["state"], "rejected")
        self.assertEqual(duplicate["error_code"], "window_exists")

    def test_overlapping_windows_rejected(self):
        result = self.service.ingest({
            "source": "test", "external_id": "w3", "kind": "window.define",
            "occurred_at": ts(1),
            "payload": {"window_id": "W3", "product": "robot-arm",
                        "window_start": midnight(10), "window_end": midnight(13), "goal_id": "G1"}})
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(result["error_code"], "window_overlap")

    def test_redefine_creates_new_boundary_version_with_reason(self):
        # Shrink W1 by one day: stays adjacent to W2, exercises versioning without overlap.
        result = self.service.ingest({
            "source": "test", "external_id": "wr1", "kind": "window.redefine",
            "occurred_at": ts(8),
            "payload": {"window_id": "W1", "window_start": midnight(5), "window_end": midnight(11),
                        "reason": "correct holiday boundary"}})
        self.assertEqual(result["state"], "accepted", result)
        window = self.service.get_window("W1")
        self.assertEqual(window["current_version"], 2)
        self.assertEqual(window["change_count"], 1)
        old = self.service.get_window("W1", 1)
        self.assertEqual(old["window_end"], norm(midnight(12)))
        self.assertEqual(len(window["versions"]), 2)

    def test_window_events_are_soft_rejected_with_reason_when_invalid(self):
        bad = self.service.ingest({
            "source": "test", "external_id": "wr-bad", "kind": "window.redefine",
            "occurred_at": ts(8),
            "payload": {"window_id": "W1", "window_start": midnight(13), "window_end": midnight(5),
                        "reason": "inverted"}})
        self.assertEqual(bad["state"], "rejected")
        self.assertEqual(bad["error_code"], "invalid_window")

    # ----- attribution and cross-week -------------------------------------
    def test_friday_integration_monday_test_stays_in_owning_window(self):
        # Job opens and integrates inside W1 (Fri Jan 9), the test lands Mon Jan 12 (W2).
        self._open("j1", "J1", 7)
        self._stage("s1", "J1", "machining", 7, 9)
        self._stage("s2", "J1", "assembly", 8, 9)
        self._stage("s3", "J1", "integration", 9, 9)
        self._test("t1", "T1", "J1", 12)
        weekly = self.service.iteration_weekly("robot-arm")
        by_id = {w["window_id"]: w for w in weekly["windows"]}
        self.assertIn("J1", by_id["W1"]["job_ids"])
        self.assertNotIn("J1", by_id["W2"]["job_ids"])
        incoming = by_id["W1"]["cross_week"]["incoming"]
        self.assertEqual([item["test_id"] for item in incoming], ["T1"])
        self.assertEqual(incoming[0]["reason"], "late_test_after_window_end")
        # The late test still counts on the owning window's chain, never double-attributed.
        chain = by_id["W1"]["attempt_chains"][0]
        self.assertEqual(chain["attempts"][0]["tests"][0]["test_id"], "T1")
        self.assertEqual(by_id["W2"]["attempt_chains"], [])

    def test_rework_attempts_explained_but_counted_once_as_delivery(self):
        # Original attempt J1 fails the threshold in W1; rework J2 (parent J1) passes in W2.
        self._open("j1", "J1", 6)
        self._stage("s1", "J1", "machining", 6, 9)
        self._stage("s2", "J1", "assembly", 6, 12)
        self._stage("s3", "J1", "integration", 7, 9)
        self._test("t1", "T1", "J1", 8, passed=1, failed=3)  # 0.25 < 0.75 -> failed
        self._open("j2", "J2", 13, parent="J1")
        self._stage("s4", "J2", "machining", 13, 9)
        self._stage("s5", "J2", "assembly", 14, 9)
        self._stage("s6", "J2", "integration", 15, 9)
        self._test("t2", "T2", "J2", 16, passed=4, failed=0)
        weekly = self.service.iteration_weekly("robot-arm")
        by_id = {w["window_id"]: w for w in weekly["windows"]}
        w1, w2 = by_id["W1"], by_id["W2"]
        # The failed original attempt is visible in W1 and is not a qualified delivery.
        self.assertEqual(w1["attempts"], 1)
        self.assertEqual(w1["rework_attempts"], 0)
        self.assertEqual(w1["qualified_prototypes"], 0)
        self.assertEqual(w1["distinct_qualified_deliveries"], 0)
        # The rework is a second attempt of the SAME root chain, not a new independent delivery.
        self.assertEqual(w2["attempts"], 1)
        chain = w2["attempt_chains"][0]
        self.assertEqual(chain["root_job_id"], "J1")
        self.assertEqual(chain["attempt_count"], 2)
        self.assertEqual(chain["rework_count"], 1)
        self.assertTrue(chain["qualified_prototype"])
        self.assertEqual(chain["final_job_id"], "J2")
        self.assertTrue(chain["attempts"][1]["is_rework"])
        self.assertEqual(w2["distinct_qualified_deliveries"], 1)
        self.assertEqual(w2["rework_attempts"], 1)

    def test_carried_over_open_job_is_pending_quality(self):
        self._open("j1", "J1", 10)  # opens inside W1, never finished
        weekly = self.service.iteration_weekly("robot-arm")
        w1 = next(w for w in weekly["windows"] if w["window_id"] == "W1")
        self.assertEqual(w1["cross_week"]["carried_over"][0]["reason"], "still_open_at_window_end")
        subjects = {item["subject"] for item in weekly["pending_quality"]}
        self.assertIn("J1", subjects)

    # ----- boundary change impact ------------------------------------------
    def test_boundary_change_lists_reassigned_jobs_and_pending_conclusions(self):
        self._open("j1", "J1", 11)  # Jan 11 inside original W1 [5,12)
        self._stage("s1", "J1", "machining", 11, 9)
        self._stage("s2", "J1", "assembly", 11, 12)
        self._stage("s3", "J1", "integration", 11, 15)  # awaiting_test, no test yet
        # Shrink W1 to end Jan 10: J1 moves out and becomes unassigned.
        self.service.ingest({
            "source": "test", "external_id": "wr1", "kind": "window.redefine",
            "occurred_at": ts(11, 8),
            "payload": {"window_id": "W1", "window_start": midnight(5), "window_end": midnight(10),
                        "reason": "correct holiday boundary"}})
        impact = self.service.boundary_impact("W1", from_version=1, to_version=2)
        self.assertEqual([item["job_id"] for item in impact["moved_out"]], ["J1"])
        self.assertEqual(impact["moved_out"][0]["reassigned_to"], "unassigned")
        self.assertEqual(impact["moved_in"], [])
        pending_subjects = {item["subject"] for item in impact["pending_quality"]}
        self.assertIn("J1", pending_subjects)

    def test_boundary_change_marks_frozen_releases_but_never_rewrites_them(self):
        self._open("j1", "J1", 7)
        self._stage("s1", "J1", "machining", 7, 9)
        self._stage("s2", "J1", "assembly", 8, 9)
        self._stage("s3", "J1", "integration", 9, 9)
        self._test("t1", "T1", "J1", 10)
        report = self.service.report("robot-arm", midnight(5), midnight(12))
        frozen = self.service.publish("week1-v1", "robot-arm", midnight(5), midnight(12), report["fingerprint"])
        # Shrink W1: no overlap with W2, but the frozen release used the old boundary.
        self.service.ingest({
            "source": "test", "external_id": "wr1", "kind": "window.redefine",
            "occurred_at": ts(11),
            "payload": {"window_id": "W1", "window_start": midnight(5), "window_end": midnight(10),
                        "reason": "correct boundary"}})
        impact = self.service.boundary_impact("W1", 1, 2)
        self.assertEqual([r["release_id"] for r in impact["affected_frozen_releases"]], ["week1-v1"])
        self.assertEqual(impact["stayed"][0]["job_id"], "J1")
        # The frozen release is byte-identical after the boundary change.
        still = self.service.get_release("week1-v1")
        self.assertEqual(still["fingerprint"], frozen["fingerprint"])
        self.assertTrue(self.service.shelf.freshness("week1-v1")["current_differs"])

    def test_late_goal_revision_does_not_rewrite_published_weekly_report(self):
        self._open("j1", "J1", 7)
        self._stage("s1", "J1", "machining", 7, 9)
        self._stage("s2", "J1", "assembly", 8, 9)
        self._stage("s3", "J1", "integration", 9, 9)
        self._test("t1", "T1", "J1", 10)
        report = self.service.report("robot-arm", midnight(5), midnight(12))
        self.service.publish("week1-v1", "robot-arm", midnight(5), midnight(12), report["fingerprint"])
        # Goal tightened after the weekly report went out.
        self.service.ingest({
            "source": "test", "external_id": "g2", "kind": "goal.define",
            "occurred_at": ts(14),
            "payload": {"goal_id": "G1", "product": "robot-arm", "title": "stricter",
                        "minimum_pass_rate": "0.9", "reason": "post-review tightening"}})
        drift = self.service.goal_drift("robot-arm")
        w1 = next(item for item in drift["windows"] if item["window_id"] == "W1")
        self.assertTrue(w1["calibration_stale"])
        self.assertEqual(w1["calibrated_threshold"], "0.75")
        self.assertEqual(w1["current_threshold"], "0.9")
        self.assertEqual(len(w1["frozen_releases"]), 1)
        # Frozen report still carries the old facts and fingerprint.
        frozen = self.service.get_release("week1-v1")
        self.assertEqual(frozen["fingerprint"], report["fingerprint"])

    # ----- traceability ----------------------------------------------------
    def test_weekly_report_traces_to_designs_jobs_tests_and_governance_events(self):
        self._open("j1", "J1", 7)
        self._stage("s1", "J1", "machining", 7, 9)
        self._stage("s2", "J1", "assembly", 8, 9)
        self._stage("s3", "J1", "integration", 9, 9)
        self._test("t1", "T1", "J1", 10)
        weekly = self.service.iteration_weekly("robot-arm")
        w1 = next(w for w in weekly["windows"] if w["window_id"] == "W1")
        trace = w1["trace"]
        self.assertEqual(trace["designs"], [{"design_id": "R1", "revision": 1}])
        self.assertEqual([job["job_id"] for job in trace["jobs"]], ["J1"])
        # Stage and test source events are reachable from the job entry.
        job_trace = trace["jobs"][0]
        self.assertTrue(job_trace["source_event_ids"])
        # Governance provenance: the window definition and goal definition events.
        self.assertIsInstance(trace["window_event_id"], int)
        self.assertIsInstance(trace["goal_event_id"], int)
        self.assertIn(trace["window_event_id"], trace["event_ids"])
        self.assertIn(trace["goal_event_id"], trace["event_ids"])

    def test_audit_covers_governance_version_chains(self):
        self._open("j1", "J1", 7)
        audit = self.service.audit("robot-arm")
        governance_codes = {finding["code"] for finding in audit["findings"]}
        self.assertTrue({"window_version_gap", "goal_version_gap", "window_overlap",
                         "window_goal_missing"}.isdisjoint(governance_codes), audit["findings"])
        self.assertTrue(audit["ok"], audit["findings"])

    def test_jobs_outside_any_window_are_reported_not_dropped(self):
        self._open("j1", "J1", 20)  # after W2 ends on Jan 19
        weekly = self.service.iteration_weekly("robot-arm")
        self.assertEqual(weekly["unassigned_jobs"],
                         [{"job_id": "J1", "opened_at": norm(ts(20))}])
