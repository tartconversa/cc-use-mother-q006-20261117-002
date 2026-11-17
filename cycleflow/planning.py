from .analytics import window
from .contracts import DomainError, MissingDependency, decimal_value, keys, required_text, utc_now


class PlanningRules:
    """Versioned iteration windows and product goals.

    Both entities are append-only chains: a redefinition never overwrites a
    prior version, so a weekly report already published keeps pointing at the
    exact window boundary and goal calibration it was computed against.
    """

    def __init__(self, db):
        self.db = db

    # ----- goals -----------------------------------------------------------
    def define_goal(self, payload, occurred_at, event_id):
        keys(payload, {"goal_id", "product", "title", "minimum_pass_rate"},
             {"goal_id", "product", "title", "minimum_pass_rate", "plan_id", "reason"})
        goal_id = required_text(payload["goal_id"], "goal_id")
        product = required_text(payload["product"], "product")
        title = required_text(payload["title"], "title")
        minimum = decimal_value(payload["minimum_pass_rate"], "minimum_pass_rate")
        if float(minimum) > 1:
            raise DomainError("invalid_threshold", "minimum_pass_rate cannot exceed one")
        plan_id = payload.get("plan_id")
        if plan_id is not None:
            plan_id = required_text(plan_id, "plan_id")
            plan = self.db.execute("SELECT product FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan is None:
                raise MissingDependency("plan", plan_id)
            if plan["product"] != product:
                raise DomainError("goal_plan_mismatch", "plan must belong to the goal product")
        latest = self.db.execute(
            "SELECT COALESCE(MAX(version),0) FROM goal_versions WHERE goal_id=?", (goal_id,)).fetchone()[0]
        if latest == 0:
            other = self.db.execute(
                "SELECT 1 FROM goal_versions WHERE product=? AND goal_id<>? LIMIT 1",
                (product, goal_id)).fetchone()
            if other is not None:
                raise DomainError("goal_product_conflict", "goal_id is already bound to another product")
        version = latest + 1
        reason = required_text(payload.get("reason", "initial goal"), "reason")
        self.db.execute(
            "INSERT INTO goal_versions(goal_id,product,version,title,minimum_pass_rate,plan_id,"
            "effective_at,reason,event_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (goal_id, product, version, title, minimum, plan_id, occurred_at, reason, event_id, utc_now()))
        return {"goal_id": goal_id, "product": product, "version": version,
                "minimum_pass_rate": minimum, "effective_at": occurred_at, "revised": version > 1}

    def get_goal(self, goal_id, version=None):
        if version is None:
            row = self.db.execute(
                "SELECT * FROM goal_versions WHERE goal_id=? ORDER BY version DESC LIMIT 1",
                (goal_id,)).fetchone()
        else:
            row = self.db.execute(
                "SELECT * FROM goal_versions WHERE goal_id=? AND version=?", (goal_id, version)).fetchone()
        if row is None:
            raise MissingDependency("goal", goal_id if version is None else f"{goal_id}:v{version}")
        return dict(row)

    def list_goals(self, product):
        rows = self.db.execute(
            "SELECT * FROM goal_versions WHERE product=? ORDER BY goal_id,version", (product,)).fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault(row["goal_id"], []).append(dict(row))
        return [{"goal_id": goal_id, "current_version": versions[-1]["version"],
                 "versions": versions} for goal_id, versions in sorted(grouped.items())]

    # ----- iteration windows ----------------------------------------------
    def define_window(self, payload, occurred_at, event_id):
        keys(payload, {"window_id", "product", "window_start", "window_end", "goal_id"},
             {"window_id", "product", "window_start", "window_end", "goal_id", "reason"})
        window_id = required_text(payload["window_id"], "window_id")
        product = required_text(payload["product"], "product")
        start, end = window(payload["window_start"], payload["window_end"])
        if self.db.execute("SELECT 1 FROM iteration_windows WHERE window_id=?", (window_id,)).fetchone():
            raise DomainError("window_exists", "window_id already defined; use window.redefine to change it", 409)
        goal = self._goal_for_product(payload["goal_id"], product)
        self._assert_no_overlap(product, start, end, exclude=None)
        reason = required_text(payload.get("reason", "initial window"), "reason")
        self.db.execute(
            "INSERT INTO iteration_windows(window_id,product,current_version,goal_id,goal_version,created_at) "
            "VALUES(?,?,1,?,?,?)",
            (window_id, product, goal["goal_id"], goal["version"], occurred_at))
        self.db.execute(
            "INSERT INTO window_versions(window_id,version,product,window_start,window_end,goal_id,"
            "goal_version,reason,supersedes_version,event_id,created_at) VALUES(?,1,?,?,?,?,?,?,NULL,?,?)",
            (window_id, product, start, end, goal["goal_id"], goal["version"], reason, event_id, utc_now()))
        return self.get_window(window_id)

    def redefine_window(self, payload, occurred_at, event_id):
        keys(payload, {"window_id", "window_start", "window_end", "reason"},
             {"window_id", "window_start", "window_end", "reason", "goal_id"})
        window_id = required_text(payload["window_id"], "window_id")
        current = self.db.execute("SELECT * FROM iteration_windows WHERE window_id=?", (window_id,)).fetchone()
        if current is None:
            raise MissingDependency("window", window_id)
        start, end = window(payload["window_start"], payload["window_end"])
        reason = required_text(payload["reason"], "reason")
        if "goal_id" in payload:
            goal = self._goal_for_product(payload["goal_id"], current["product"])
        else:
            goal = self.get_goal(current["goal_id"], current["goal_version"])
        self._assert_no_overlap(current["product"], start, end, exclude=window_id)
        version = current["current_version"] + 1
        self.db.execute(
            "INSERT INTO window_versions(window_id,version,product,window_start,window_end,goal_id,"
            "goal_version,reason,supersedes_version,event_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (window_id, version, current["product"], start, end, goal["goal_id"], goal["version"],
             reason, current["current_version"], event_id, occurred_at))
        self.db.execute(
            "UPDATE iteration_windows SET current_version=?,goal_id=?,goal_version=? WHERE window_id=?",
            (version, goal["goal_id"], goal["version"], window_id))
        return self.get_window(window_id)

    def get_window(self, window_id, version=None):
        root = self.db.execute("SELECT * FROM iteration_windows WHERE window_id=?", (window_id,)).fetchone()
        if root is None:
            raise MissingDependency("window", window_id)
        wanted = version if version is not None else root["current_version"]
        boundary = self.db.execute(
            "SELECT * FROM window_versions WHERE window_id=? AND version=?", (window_id, wanted)).fetchone()
        if boundary is None:
            raise MissingDependency("window version", f"{window_id}:v{wanted}")
        versions = [dict(row) for row in self.db.execute(
            "SELECT * FROM window_versions WHERE window_id=? ORDER BY version", (window_id,))]
        result = dict(root)
        result["window_start"] = boundary["window_start"]
        result["window_end"] = boundary["window_end"]
        result["goal_version"] = boundary["goal_version"]
        result["goal_id"] = boundary["goal_id"]
        result["versions"] = versions
        result["change_count"] = len(versions) - 1
        return result

    def list_windows(self, product):
        rows = self.db.execute(
            "SELECT window_id FROM iteration_windows WHERE product=? ORDER BY created_at,window_id",
            (product,)).fetchall()
        return [self.get_window(row["window_id"]) for row in rows]

    def window_for(self, product, moment):
        """Resolve the current window whose boundary contains ``moment``."""
        row = self.db.execute(
            "SELECT w.window_id FROM iteration_windows w JOIN window_versions v "
            "ON v.window_id=w.window_id AND v.version=w.current_version "
            "WHERE w.product=? AND v.window_start<=? AND v.window_end>? AND w.state='active'",
            (product, moment)).fetchone()
        return self.get_window(row["window_id"]) if row else None

    def _goal_for_product(self, goal_id, product):
        goal = self.get_goal(goal_id)
        if goal["product"] != product:
            raise DomainError("goal_product_mismatch", "goal must belong to the window product")
        return goal

    def _assert_no_overlap(self, product, start, end, exclude):
        rows = self.db.execute(
            "SELECT w.window_id,v.window_start,v.window_end FROM iteration_windows w "
            "JOIN window_versions v ON v.window_id=w.window_id AND v.version=w.current_version "
            "WHERE w.product=? AND w.state='active'", (product,)).fetchall()
        for row in rows:
            if exclude and row["window_id"] == exclude:
                continue
            if start < row["window_end"] and row["window_start"] < end:
                raise DomainError(
                    "window_overlap",
                    f"boundary overlaps window {row['window_id']} [{row['window_start']},{row['window_end']})", 409)

    def dispatch(self, kind, payload, occurred_at, event_id):
        if kind == "goal.define":
            return self.define_goal(payload, occurred_at, event_id)
        if kind == "window.define":
            return self.define_window(payload, occurred_at, event_id)
        if kind == "window.redefine":
            return self.redefine_window(payload, occurred_at, event_id)
        raise DomainError("unknown_kind", f"unsupported planning event {kind}")
