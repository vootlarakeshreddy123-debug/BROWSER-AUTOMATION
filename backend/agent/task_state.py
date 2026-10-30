"""Structured task state for the goal runner (no single global "done" boolean)."""
from __future__ import annotations

from dataclasses import dataclass, field

from agent.goals import Goal, GoalStatus


@dataclass
class TaskState:
    instruction: str
    goals: list[Goal]
    current_goal_index: int = 0
    action_history: list[dict] = field(default_factory=list)
    last_search_query: str | None = None
    last_search_outcome: str | None = None
    url_history: dict[int, list[str]] = field(default_factory=dict)       # page_id -> urls we observed, in order
    forward_stack: dict[int, list[str]] = field(default_factory=dict)
    progress_count: int = 0
    no_progress_count: int = 0
    false_done_count: int = 0
    recovery_count: int = 0
    extracted: list[dict] = field(default_factory=list)

    @property
    def completed_goals(self) -> list[Goal]:
        return [g for g in self.goals if g.status == GoalStatus.COMPLETED]

    @property
    def current_goal(self) -> Goal | None:
        return self.goals[self.current_goal_index] if 0 <= self.current_goal_index < len(self.goals) else None

    def track_url(self, page_id: int, url: str) -> None:
        hist = self.url_history.setdefault(page_id, [])
        if not hist or hist[-1] != url:
            hist.append(url)
            del hist[:-30]

    def expected_back(self, page_id: int) -> str | None:
        hist = self.url_history.get(page_id) or []
        return hist[-2] if len(hist) >= 2 else None

    def expected_forward(self, page_id: int) -> str | None:
        fs = self.forward_stack.get(page_id) or []
        return fs[-1] if fs else None

    def did_back(self, page_id: int) -> None:
        hist = self.url_history.setdefault(page_id, [])
        if len(hist) >= 2:
            self.forward_stack.setdefault(page_id, []).append(hist.pop())

    def did_forward(self, page_id: int) -> None:
        fs = self.forward_stack.setdefault(page_id, [])
        if fs:
            u = fs.pop()
            hist = self.url_history.setdefault(page_id, [])
            if not hist or hist[-1] != u:
                hist.append(u)

    def summary(self) -> list[dict]:
        return [g.to_dict() for g in self.goals]
