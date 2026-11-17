import json
import tempfile
from pathlib import Path

from .audit import IntegrityAudit
from .service import CycleService


DAY = "2026-01-12"
START = "2026-01-01T00:00:00Z"
END = "2026-02-01T00:00:00Z"


def event(number, kind, minute, payload, source="demo"):
    return {"source": source, "external_id": str(number), "kind": kind,
            "occurred_at": f"{DAY}T09:{minute:02d}:00Z", "payload": payload}


def scenario():
    return [
        event(1, "design.release", 0, {"design_id": "R1", "product": "robot-arm",
                                       "revision": 1, "parts": {"motor": 2, "sensor": 1},
                                       "specification": {"load_kg": 5}}),
        event(2, "lot.receive", 1, {"lot_id": "M1", "part": "motor",
                                     "supplier": "local-motor", "quantity": 8}),
        event(3, "lot.receive", 2, {"lot_id": "S1", "part": "sensor",
                                     "supplier": "local-sensor", "quantity": 4}),
        event(4, "plan.create", 3, {"plan_id": "P1", "product": "robot-arm",
                                     "revision": 1, "minimum_pass_rate": "0.75"}),
        event(5, "job.open", 4, {"job_id": "J1", "design_id": "R1", "quantity": 4}),
        event(6, "job.stage_start", 5, {"job_id": "J1", "stage": "machining"}),
        event(7, "job.stage_finish", 10, {"job_id": "J1", "stage": "machining"}),
        event(8, "job.stage_start", 12, {"job_id": "J1", "stage": "assembly"}),
        event(9, "job.stage_finish", 20, {"job_id": "J1", "stage": "assembly"}),
        event(10, "job.stage_start", 21, {"job_id": "J1", "stage": "integration"}),
        event(11, "job.stage_finish", 27, {"job_id": "J1", "stage": "integration"}),
        event(12, "test.record", 30, {"test_id": "T1", "job_id": "J1",
                                      "plan_id": "P1", "passed": 3, "failed": 1,
                                      "note": "Synthetic local fixture"}),
    ]


def governance_scenario():
    """Cross-week test, rework chain, boundary redefinition and goal drift."""
    base = "2026-03-"
    events = [
        {"source": "gov", "external_id": "g-design", "kind": "design.release",
         "occurred_at": base + "01T09:00:00Z",
         "payload": {"design_id": "G-R1", "product": "gov-arm", "revision": 1,
                     "parts": {"motor": 1, "sensor": 1}}},
        {"source": "gov", "external_id": "g-motor", "kind": "lot.receive",
         "occurred_at": base + "01T09:01:00Z",
         "payload": {"lot_id": "G-M1", "part": "motor", "supplier": "acme", "quantity": 12}},
        {"source": "gov", "external_id": "g-sensor", "kind": "lot.receive",
         "occurred_at": base + "01T09:02:00Z",
         "payload": {"lot_id": "G-S1", "part": "sensor", "supplier": "acme", "quantity": 12}},
        {"source": "gov", "external_id": "g-plan", "kind": "plan.create",
         "occurred_at": base + "01T09:03:00Z",
         "payload": {"plan_id": "G-P1", "product": "gov-arm", "revision": 1,
                     "minimum_pass_rate": "0.75"}},
        {"source": "gov", "external_id": "g-goal", "kind": "goal.define",
         "occurred_at": base + "01T09:04:00Z",
         "payload": {"goal_id": "G-G1", "product": "gov-arm", "title": "march weekly target",
                     "minimum_pass_rate": "0.75", "plan_id": "G-P1"}},
        {"source": "gov", "external_id": "g-w1", "kind": "window.define",
         "occurred_at": base + "01T09:05:00Z",
         "payload": {"window_id": "G-W1", "product": "gov-arm",
                     "window_start": base + "02T00:00:00Z", "window_end": base + "09T00:00:00Z",
                     "goal_id": "G-G1"}},
        {"source": "gov", "external_id": "g-w2", "kind": "window.define",
         "occurred_at": base + "01T09:06:00Z",
         "payload": {"window_id": "G-W2", "product": "gov-arm",
                     "window_start": base + "09T00:00:00Z", "window_end": base + "16T00:00:00Z",
                     "goal_id": "G-G1"}},
        # J1 opens inside G-W1, integrates Friday, test lands the following Monday.
        {"source": "gov", "external_id": "g-j1", "kind": "job.open",
         "occurred_at": base + "03T09:00:00Z",
         "payload": {"job_id": "G-J1", "design_id": "G-R1", "quantity": 2}},
        {"source": "gov", "external_id": "g-j1-ms", "kind": "job.stage_start",
         "occurred_at": base + "03T10:00:00Z",
         "payload": {"job_id": "G-J1", "stage": "machining"}},
        {"source": "gov", "external_id": "g-j1-mf", "kind": "job.stage_finish",
         "occurred_at": base + "03T11:00:00Z",
         "payload": {"job_id": "G-J1", "stage": "machining"}},
        {"source": "gov", "external_id": "g-j1-as", "kind": "job.stage_start",
         "occurred_at": base + "04T09:00:00Z",
         "payload": {"job_id": "G-J1", "stage": "assembly"}},
        {"source": "gov", "external_id": "g-j1-af", "kind": "job.stage_finish",
         "occurred_at": base + "04T10:00:00Z",
         "payload": {"job_id": "G-J1", "stage": "assembly"}},
        {"source": "gov", "external_id": "g-j1-is", "kind": "job.stage_start",
         "occurred_at": base + "06T09:00:00Z",
         "payload": {"job_id": "G-J1", "stage": "integration"}},
        {"source": "gov", "external_id": "g-j1-if", "kind": "job.stage_finish",
         "occurred_at": base + "06T11:00:00Z",
         "payload": {"job_id": "G-J1", "stage": "integration"}},
        # Late test on Monday Mar 9, after the window end at midnight.
        {"source": "gov", "external_id": "g-t1", "kind": "test.record",
         "occurred_at": base + "09T10:00:00Z",
         "payload": {"test_id": "G-T1", "job_id": "G-J1", "plan_id": "G-P1",
                     "passed": 0, "failed": 2, "note": "threshold miss"}},
        # Rework of the failed attempt.
        {"source": "gov", "external_id": "g-j2", "kind": "job.open",
         "occurred_at": base + "10T09:00:00Z",
         "payload": {"job_id": "G-J2", "design_id": "G-R1", "quantity": 2,
                     "parent_job": "G-J1"}},
        {"source": "gov", "external_id": "g-j2-ms", "kind": "job.stage_start",
         "occurred_at": base + "10T10:00:00Z",
         "payload": {"job_id": "G-J2", "stage": "machining"}},
        {"source": "gov", "external_id": "g-j2-mf", "kind": "job.stage_finish",
         "occurred_at": base + "10T11:00:00Z",
         "payload": {"job_id": "G-J2", "stage": "machining"}},
        {"source": "gov", "external_id": "g-j2-as", "kind": "job.stage_start",
         "occurred_at": base + "11T09:00:00Z",
         "payload": {"job_id": "G-J2", "stage": "assembly"}},
        {"source": "gov", "external_id": "g-j2-af", "kind": "job.stage_finish",
         "occurred_at": base + "11T10:00:00Z",
         "payload": {"job_id": "G-J2", "stage": "assembly"}},
        {"source": "gov", "external_id": "g-j2-is", "kind": "job.stage_start",
         "occurred_at": base + "12T09:00:00Z",
         "payload": {"job_id": "G-J2", "stage": "integration"}},
        {"source": "gov", "external_id": "g-j2-if", "kind": "job.stage_finish",
         "occurred_at": base + "12T11:00:00Z",
         "payload": {"job_id": "G-J2", "stage": "integration"}},
        {"source": "gov", "external_id": "g-t2", "kind": "test.record",
         "occurred_at": base + "12T14:00:00Z",
         "payload": {"test_id": "G-T2", "job_id": "G-J2", "plan_id": "G-P1",
                     "passed": 2, "failed": 0, "note": "rework passes"}},
    ]
    return events


def run():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "cycleflow.sqlite3"
        service = CycleService(db)
        first = service.ingest_batch("sample", scenario(), stop_after=5)
        assert first["next_line"] == 5
        rest = service.ingest_batch("sample", scenario())
        assert rest["completed"]
        report = service.report("robot-arm", START, END)
        assert report["summary"]["passed_units"] == 3
        assert report["quality"]["publishable"]
        frozen = service.publish("jan-demo", "robot-arm", START, END, report["fingerprint"])
        assert frozen["report"]["summary"]["pass_rate"] == "0.75"

        # Governance acceptance: frozen windows, attempt chains and boundary impact.
        gov_events = governance_scenario()
        outcomes = [service.ingest(item) for item in gov_events]
        assert all(item["state"] == "accepted" for item in outcomes), outcomes
        weekly = service.iteration_weekly("gov-arm")
        by_window = {item["window_id"]: item for item in weekly["windows"]}
        window = by_window["G-W1"]
        assert window["cross_week"]["incoming"][0]["test_id"] == "G-T1"
        chain = window["attempt_chains"][0]
        # The original failed attempt is recorded in week 1, not counted as a delivery.
        assert chain["attempt_count"] == 2 and chain["rework_count"] == 1
        assert window["attempts"] == 1 and window["qualified_prototypes"] == 0
        # The rework attempt belongs to week 2 and qualifies the single root chain once.
        second = by_window["G-W2"]
        assert second["attempts"] == 1 and second["rework_attempts"] == 1
        assert second["distinct_qualified_deliveries"] == 1
        assert second["attempt_chains"][0]["final_job_id"] == "G-J2"
        assert window["trace"]["window_event_id"] and window["trace"]["goal_event_id"]
        # Redefine the boundary and prove the impact analysis is explicit.
        service.ingest({
            "source": "gov", "external_id": "g-w1-v2", "kind": "window.redefine",
            "occurred_at": "2026-03-08T12:00:00Z",
            "payload": {"window_id": "G-W1",
                        "window_start": "2026-03-02T00:00:00Z",
                        "window_end": "2026-03-03T00:00:00Z",
                        "reason": "holiday correction"}})
        impact = service.boundary_impact("G-W1", 1, 2)
        assert [item["job_id"] for item in impact["moved_out"]] == ["G-J1"]
        assert impact["moved_out"][0]["reassigned_to"] == "unassigned"
        # A late goal revision never silently rewrites anything.
        service.ingest({
            "source": "gov", "external_id": "g-goal-v2", "kind": "goal.define",
            "occurred_at": "2026-03-15T09:00:00Z",
            "payload": {"goal_id": "G-G1", "product": "gov-arm", "title": "stricter target",
                        "minimum_pass_rate": "0.9", "reason": "post-review tightening"}})
        drift = service.goal_drift("gov-arm")
        assert any(item["calibration_stale"] for item in drift["windows"])

        audit = IntegrityAudit(service.store).run("robot-arm")
        assert audit["ok"], audit
        gov_audit = IntegrityAudit(service.store).run("gov-arm")
        assert gov_audit["ok"], gov_audit
        return {"batch": {"first_cursor": first["next_line"], "complete": rest["completed"]},
                "report": report["summary"], "quality": report["quality"],
                "release_id": frozen["release_id"], "audit": audit,
                "governance": {"weekly_windows": len(weekly["windows"]),
                               "attempt_chain": chain, "boundary_impact": impact,
                               "goal_drift": drift}}


def main():
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
