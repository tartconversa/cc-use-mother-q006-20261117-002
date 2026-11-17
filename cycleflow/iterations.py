from collections import defaultdict

from .contracts import DomainError, required_text
from .planning import PlanningRules

# Job states that mean the physical work of the job is finished.
TERMINAL_STATES = {"completed", "failed", "scrapped"}


class IterationView:
    """Weekly iteration view built on frozen window boundaries and attempt chains.

    Attribution rule (single source of truth): every job belongs to the
    iteration window whose [start, end) contains its ``opened_at``. Completion
    and late tests never move the job; they are surfaced as cross-week
    carry-over instead, so "integrated Friday, tested Monday" stays explainable.
    """

    def __init__(self, store):
        self.store = store

    def _planning(self, db):
        return PlanningRules(db)

    # ----- primitives ------------------------------------------------------
    def _product_jobs(self, db, product):
        return [dict(row) for row in db.execute(
            "SELECT j.*,d.revision FROM jobs j JOIN designs d ON j.design_id=d.design_id "
            "WHERE d.product=? ORDER BY j.opened_at,j.job_id", (product,))]

    def _root_map(self, db, jobs):
        """Map every job_id to the root job of its rework lineage."""
        roots = {}

        def root_of(job_id):
            if job_id in roots:
                return roots[job_id]
            current, seen = job_id, set()
            while True:
                if current in seen:
                    raise DomainError("cyclic_rework", "job lineage is cyclic")
                seen.add(current)
                parent = db.execute(
                    "SELECT parent_job FROM jobs WHERE job_id=?", (current,)).fetchone()
                if parent is None or parent["parent_job"] is None:
                    roots[job_id] = current
                    return current
                current = parent["parent_job"]

        for job in jobs:
            root_of(job["job_id"])
        return roots

    def _tests_for(self, db, job_id):
        return [dict(row) for row in db.execute(
            "SELECT * FROM tests WHERE job_id=? ORDER BY tested_at,test_id", (job_id,))]

    def _job_event_ids(self, db, job_id):
        ids = [row[0] for row in db.execute(
            "SELECT event_id FROM job_steps WHERE job_id=?", (job_id,))]
        ids.extend(row[0] for row in db.execute(
            "SELECT event_id FROM tests WHERE job_id=?", (job_id,)))
        return sorted(set(ids))

    def _attribute(self, boundaries, moment):
        for boundary in boundaries:
            if boundary["start"] <= moment < boundary["end"]:
                return boundary["window_id"]
        return None

    def _attempt_chain(self, db, root_id, members):
        attempts = []
        for job in sorted(members, key=lambda item: item["opened_at"]):
            tests = self._tests_for(db, job["job_id"])
            attempts.append({
                "job_id": job["job_id"], "revision": job["revision"],
                "status": job["status"], "opened_at": job["opened_at"],
                "completed_at": job["completed_at"], "parent_job": job["parent_job"],
                "failure_reason": job["failure_reason"], "is_rework": job["parent_job"] is not None,
                "tests": [{"test_id": t["test_id"], "plan_id": t["plan_id"],
                           "tested_at": t["tested_at"], "passed": t["passed"],
                           "failed": t["failed"], "source_event_id": t["event_id"]} for t in tests],
                "source_event_ids": self._job_event_ids(db, job["job_id"])})
        final = attempts[-1]
        return {"root_job_id": root_id, "attempt_count": len(attempts),
                "rework_count": len(attempts) - 1, "final_status": final["status"],
                "qualified_prototype": final["status"] == "completed",
                "final_job_id": final["job_id"], "attempts": attempts}

    # ----- weekly iteration report ----------------------------------------
    def weekly(self, product):
        product = required_text(product, "product")
        with self.store.read() as db:
            planning = self._planning(db)
            windows = planning.list_windows(product)
            active = [{"window_id": w["window_id"], "start": w["window_start"],
                       "end": w["window_end"], "version": w["current_version"]} for w in windows]
            jobs = self._product_jobs(db, product)
            roots = self._root_map(db, jobs)
            members_by_root = defaultdict(list)
            for job in jobs:
                members_by_root[roots[job["job_id"]]].append(job)
            chains = {root: self._attempt_chain(db, root, members)
                      for root, members in members_by_root.items()}

            buckets = {w["window_id"]: self._empty_window(w) for w in windows}
            unassigned = []
            for job in jobs:
                window_id = self._attribute(active, job["opened_at"])
                if window_id is None:
                    unassigned.append({"job_id": job["job_id"], "opened_at": job["opened_at"]})
                    continue
                boundary = next(b for b in active if b["window_id"] == window_id)
                buckets[window_id]["job_ids"].append(job["job_id"])
                self._classify_cross_week(db, buckets[window_id], job, boundary)

            result = []
            for window in windows:
                bucket = buckets[window["window_id"]]
                self._fill_window(db, planning, bucket, chains, roots)
                result.append(bucket)
            result.sort(key=lambda item: item["window_start"])
            return {"product": product,
                    "attribution": "job.opened_at inside window [start,end); completion and late "
                                   "tests never re-attribute the job",
                    "windows": result, "unassigned_jobs": unassigned,
                    "pending_quality": self._pending_quality(db, product),
                    "source_head": db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]}

    def _empty_window(self, window):
        return {"window_id": window["window_id"], "version": window["current_version"],
                "window_start": window["window_start"], "window_end": window["window_end"],
                "goal_id": window["goal_id"], "goal_version": window["goal_version"],
                "job_ids": [], "attempt_chains": [],
                "cross_week": {"carried_over": [], "incoming": []},
                "source_event_ids": set()}

    def _classify_cross_week(self, db, bucket, job, boundary):
        if job["completed_at"] is not None and job["completed_at"] >= boundary["end"]:
            bucket["cross_week"]["carried_over"].append(
                {"job_id": job["job_id"], "opened_at": job["opened_at"],
                 "completed_at": job["completed_at"], "reason": "completion_after_window_end"})
        elif job["completed_at"] is None and job["status"] not in TERMINAL_STATES:
            bucket["cross_week"]["carried_over"].append(
                {"job_id": job["job_id"], "opened_at": job["opened_at"],
                 "completed_at": None, "reason": "still_open_at_window_end"})
        for test in db.execute(
                "SELECT test_id,tested_at,passed,failed FROM tests WHERE job_id=? ORDER BY test_id",
                (job["job_id"],)):
            if test["tested_at"] >= boundary["end"]:
                bucket["cross_week"]["incoming"].append(
                    {"job_id": job["job_id"], "test_id": test["test_id"],
                     "tested_at": test["tested_at"], "reason": "late_test_after_window_end",
                     "passed": test["passed"], "failed": test["failed"]})

    def _fill_window(self, db, planning, bucket, chains, roots):
        root_ids_in_window = sorted({roots[job_id] for job_id in bucket["job_ids"]})
        window_chains, attempts, rework, qualified = [], 0, 0, 0
        for root_id in root_ids_in_window:
            chain = chains[root_id]
            window_chains.append(chain)
            # Only attempts whose job opened inside this window are counted as
            # window activity; the full chain history stays visible in the payload.
            for attempt in chain["attempts"]:
                if attempt["job_id"] in bucket["job_ids"]:
                    attempts += 1
                    if attempt["is_rework"]:
                        rework += 1
            # A root chain qualifies here only if its final job belongs to this window.
            if chain["qualified_prototype"] and chain["final_job_id"] in bucket["job_ids"]:
                qualified += 1
        goal = planning.get_goal(bucket["goal_id"], bucket["goal_version"])
        bucket["attempt_chains"] = window_chains
        bucket["chains_started"] = len(root_ids_in_window)
        bucket["attempts"] = attempts
        bucket["rework_attempts"] = rework
        bucket["qualified_prototypes"] = qualified
        # Independent delivery: each root chain counts once no matter how many rework attempts it took.
        bucket["distinct_qualified_deliveries"] = qualified
        bucket["incoming_test_roots"] = sorted(
            {roots[item["job_id"]] for item in bucket["cross_week"]["incoming"]})
        bucket["goal"] = {"goal_id": goal["goal_id"], "version": goal["version"],
                          "title": goal["title"], "minimum_pass_rate": goal["minimum_pass_rate"],
                          "effective_at": goal["effective_at"]}
        bucket["cross_week"]["carried_over"].sort(key=lambda item: item["job_id"])
        bucket["cross_week"]["incoming"].sort(key=lambda item: (item["job_id"], item["test_id"]))
        bucket["job_ids"].sort()
        for chain in window_chains:
            for attempt in chain["attempts"]:
                bucket["source_event_ids"].update(attempt["source_event_ids"])
        window_event = db.execute(
            "SELECT event_id,reason FROM window_versions WHERE window_id=? AND version=?",
            (bucket["window_id"], bucket["version"])).fetchone()
        goal_event = db.execute(
            "SELECT event_id,reason FROM goal_versions WHERE goal_id=? AND version=?",
            (bucket["goal_id"], bucket["goal_version"])).fetchone()
        for row in (window_event, goal_event):
            if row and row["event_id"]:
                bucket["source_event_ids"].add(row["event_id"])
        bucket["source_event_ids"] = sorted(bucket["source_event_ids"])
        bucket["trace"] = self._trace(db, bucket, window_event, goal_event)

    def _trace(self, db, bucket, window_event, goal_event):
        """Every weekly figure can be walked back to designs, jobs, tests, events."""
        designs, seen = [], set()
        for job_id in bucket["job_ids"]:
            row = db.execute(
                "SELECT d.design_id,d.revision FROM jobs j JOIN designs d ON d.design_id=j.design_id "
                "WHERE j.job_id=?", (job_id,)).fetchone()
            if row and row["design_id"] not in seen:
                seen.add(row["design_id"])
                designs.append({"design_id": row["design_id"], "revision": row["revision"]})
        return {
            "window_event_id": window_event["event_id"], "window_reason": window_event["reason"],
            "goal_event_id": goal_event["event_id"], "goal_reason": goal_event["reason"],
            "designs": sorted(designs, key=lambda item: item["design_id"]),
            "jobs": [{"job_id": job_id, "source_event_ids": self._job_event_ids(db, job_id)}
                     for job_id in bucket["job_ids"]],
            "event_ids": bucket["source_event_ids"]}

    # ----- pending quality conclusions ------------------------------------
    def _pending_quality(self, db, product):
        pending = []
        rows = db.execute(
            "SELECT j.job_id,j.status FROM jobs j JOIN designs d ON d.design_id=j.design_id "
            "WHERE d.product=? ORDER BY j.job_id", (product,))
        for job in rows:
            if job["status"] == "awaiting_test":
                pending.append({"code": "missing_test", "subject": job["job_id"],
                                "detail": "integration finished; quality conclusion waits on test.record"})
            elif job["status"] in {"open", "in_progress"}:
                pending.append({"code": "unfinished_job", "subject": job["job_id"],
                                "detail": "work still open; no quality conclusion possible yet"})
            elif job["status"] == "completed":
                if db.execute("SELECT 1 FROM tests WHERE job_id=?", (job["job_id"],)).fetchone() is None:
                    pending.append({"code": "unexplained_completion", "subject": job["job_id"],
                                    "detail": "completed without any recorded test"})
        return pending

    # ----- boundary change impact ------------------------------------------
    def boundary_impact(self, window_id, from_version, to_version=None):
        """Explain what a window boundary change re-attributes.

        Both boundaries are replayed against current facts, so the answer is
        reproducible and auditable; frozen releases are never rewritten, only
        listed as affected.
        """
        with self.store.read() as db:
            planning = self._planning(db)
            window = planning.get_window(window_id)
            product = window["product"]
            old = planning.get_window(window_id, from_version)
            new = planning.get_window(window_id, to_version or window["current_version"])
            jobs = self._product_jobs(db, product)
            other_windows = [{"window_id": w["window_id"], "start": w["window_start"],
                              "end": w["window_end"]}
                             for w in planning.list_windows(product) if w["window_id"] != window_id]

            def inside(job, boundary):
                return boundary["window_start"] <= job["opened_at"] < boundary["window_end"]

            touched = set()
            moved_in, moved_out, stayed = [], [], []
            for job in jobs:
                was, now = inside(job, old), inside(job, new)
                if not was and not now:
                    continue
                touched.add(job["job_id"])
                entry = {"job_id": job["job_id"], "opened_at": job["opened_at"],
                         "status": job["status"], "parent_job": job["parent_job"]}
                if was and not now:
                    entry["reassigned_to"] = self._attribute(other_windows, job["opened_at"]) or "unassigned"
                    moved_out.append(entry)
                elif now and not was:
                    entry["reassigned_from"] = self._attribute(other_windows, job["opened_at"]) or "unassigned"
                    moved_in.append(entry)
                else:
                    stayed.append(entry)
            affected_releases = [dict(row) for row in db.execute(
                "SELECT release_id,window_start,window_end,fingerprint,source_head FROM releases "
                "WHERE product=? AND window_start=? AND window_end=?",
                (product, old["window_start"], old["window_end"]))]
            pending = [item for item in self._pending_quality(db, product) if item["subject"] in touched]
            return {
                "window_id": window_id, "product": product,
                "from_version": from_version, "to_version": new["current_version"],
                "old_boundary": {"start": old["window_start"], "end": old["window_end"],
                                 "goal_id": old["goal_id"], "goal_version": old["goal_version"]},
                "new_boundary": {"start": new["window_start"], "end": new["window_end"],
                                 "goal_id": new["goal_id"], "goal_version": new["goal_version"]},
                "goal_calibration_changed": (old["goal_id"], old["goal_version"]) !=
                                            (new["goal_id"], new["goal_version"]),
                "moved_in": sorted(moved_in, key=lambda item: item["job_id"]),
                "moved_out": sorted(moved_out, key=lambda item: item["job_id"]),
                "stayed": sorted(stayed, key=lambda item: item["job_id"]),
                "pending_quality": pending,
                "affected_frozen_releases": affected_releases,
                "note": "frozen releases keep their original report and fingerprint; re-publishing "
                        "under the new boundary creates a new release_id"}

    # ----- goal revision drift against frozen releases ---------------------
    def goal_drift(self, product):
        """Show the goal calibration frozen per window against the current goal."""
        with self.store.read() as db:
            planning = self._planning(db)
            drift = []
            for window in planning.list_windows(product):
                current_goal = planning.get_goal(window["goal_id"])
                used = planning.get_goal(window["goal_id"], window["goal_version"])
                releases = [dict(row) for row in db.execute(
                    "SELECT release_id,fingerprint,source_head FROM releases "
                    "WHERE product=? AND window_start=? AND window_end=?",
                    (product, window["window_start"], window["window_end"]))]
                drift.append({
                    "window_id": window["window_id"], "version": window["current_version"],
                    "goal_id": window["goal_id"],
                    "calibrated_goal_version": window["goal_version"],
                    "current_goal_version": current_goal["version"],
                    "calibrated_threshold": used["minimum_pass_rate"],
                    "current_threshold": current_goal["minimum_pass_rate"],
                    "calibration_stale": window["goal_version"] != current_goal["version"],
                    "frozen_releases": releases,
                    "note": "late goal revisions never alter frozen releases; re-publish explicitly "
                            "if the new calibration must apply"})
            return {"product": product, "windows": drift}
