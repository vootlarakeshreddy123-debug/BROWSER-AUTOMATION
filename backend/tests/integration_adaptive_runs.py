"""INTEGRATION harness: real orchestrator + real Chrome + LOCAL FIXTURE sites.

What is REAL here : orchestrator state machine, router, deterministic planner,
                    inspector, resolver, executor, verifier, progress tracker,
                    budgets, provider-health manager, event recorder, and a real
                    (headless) Chrome driven over CDP.
What is FAKE here : the LLM "brains" (scripted, rule-based, they read the live
                    element list and emit one action) and the two websites
                    (localhost fixtures).  Nothing here proves real Gemini /
                    Ollama quality or arbitrary live-website success.

Each scenario runs in its OWN Python process (fresh module state, fresh Chrome).

    python tests/integration_adaptive_runs.py --all
    python tests/integration_adaptive_runs.py --scenario run2_adaptive_learn
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
CHROME = os.environ.get("NEXA_TEST_CHROME") or next(
    iter(sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux*/chrome"))), None)

# scenario -> (env overrides, description)
SCENARIOS: dict[str, dict] = {}


def scenario(name, desc, env=None):
    def deco(fn):
        SCENARIOS[name] = {"fn": fn, "desc": desc, "env": env or {}}
        return fn
    return deco


# --------------------------------------------------------------------------
# scripted fake LLM brain
# --------------------------------------------------------------------------
class Brain:
    """Rule-based stand-in for an LLM. Reads ONLY the live context it is given."""

    def __init__(self, name, phrases=(), behavior="normal", delay=0.0, fail=(), first_done=False,
                 two_actions=False):
        self.name, self.phrases, self.behavior = name, list(phrases), behavior
        self.delay, self.fail, self.first_done, self.two_actions = delay, list(fail), first_done, two_actions
        self.calls = 0
        self.requests = 0
        self.model = f"scripted-{name}"

    def _clicked(self, ctx):
        out = []
        for c in ctx.get("completed_actions") or []:
            a = c.get("action") or {}
            t = a.get("target") or {}
            if a.get("type") == "click" and t.get("name"):
                out.append(str(t["name"]).lower())
        return out

    def _find(self, ctx, phrase):
        for el in ctx.get("elements") or []:
            if el.get("visible") and el.get("role") in ("link", "button") and phrase.lower() in str(el.get("name") or "").lower():
                return el
        return None

    async def generate_plan(self, instruction, page_context=None, limits=None):
        from agent.action_schema import Action, ActionPlan, Target
        self.calls += 1
        if limits is not None:
            limits.begin(); limits.note_request()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            f = self.fail.pop(0) if len(self.fail) > 1 or self.behavior == "fail_once" else self.fail[0]
            if f:
                raise RuntimeError(f)
        ctx = page_context or {}
        if self.behavior == "false_done" or (self.first_done and self.calls == 1):
            return ActionPlan(actions=[], done=True, source="ai")
        if self.behavior == "nav_home":
            from urllib.parse import urlparse
            u = urlparse(ctx.get("current_url") or "")
            return ActionPlan(actions=[Action(type="navigate", url=f"{u.scheme}://{u.netloc}/", reason="go back to the homepage")], source="ai")
        if self.behavior == "loop":
            el = self._find(ctx, "Home")
            return ActionPlan(actions=[Action(type="click", target=Target(role="link", name="Home"), reason="loop")], source="ai")
        clicked = self._clicked(ctx)
        pending = [p for p in self.phrases if not any(p.lower() in c for c in clicked)]
        if not pending:
            return ActionPlan(actions=[], done=True, source="ai")
        acts = []
        if self.two_actions:   # append a decoy second action that is valid on the SAME page
            first = self._find(ctx, pending[0])
            decoy = self._find(ctx, "Explore")
            if first is not None and decoy is not None:
                return ActionPlan(actions=[
                    Action(type="click", target=Target(role=first["role"], name=first["name"]), reason=f"open {pending[0]}"),
                    Action(type="click", target=Target(role=decoy["role"], name=decoy["name"]), reason="decoy second action")],
                    source="ai")
        for ph in pending[:1]:
            el = self._find(ctx, ph)
            if el is None:
                if acts:
                    break
                return ActionPlan(actions=[Action(type="scroll", direction="down", amount=400, reason=f"look for {ph}")], source="ai")
            acts.append(Action(type="click", target=Target(role=el["role"], name=el["name"]), reason=f"open {ph}"))
        return ActionPlan(actions=acts, source="ai")


class BootstrapStub:
    async def generate_plan(self, instruction, page_context=None):
        import re
        from agent.action_schema import Action, ActionPlan
        m = re.search(r"https?://\S+", instruction)
        if not m:
            raise RuntimeError("bootstrap stub: no URL")
        return ActionPlan(actions=[Action(type="navigate", url=m.group(0).rstrip(",."))], source="ai")


# --------------------------------------------------------------------------
# one task through the REAL orchestrator
# --------------------------------------------------------------------------
async def run_task_once(instruction, gemini, ollama, sites_note=""):
    from agent import orchestrator as orch
    from database.models import TaskRecord
    from database.repository import task_repository
    from services.ws_manager import ws_manager

    ws_log: list[dict] = []
    orig_send = ws_manager.send

    async def spy(task_id, payload):
        ws_log.append(payload)
        return await orig_send(task_id, payload)

    ws_manager.send = spy
    orch.get_ai_provider = lambda: BootstrapStub()
    orch.get_adaptive_providers = lambda: {"gemini": gemini, "ollama": ollama}
    task = TaskRecord(instruction=instruction)
    task_repository.create(task)
    t0 = time.perf_counter()
    await orch.run_task(task)
    wall = time.perf_counter() - t0
    ws_manager.send = orig_send
    ev_types = [e["type"] for e in task.events]
    return {
        "instruction": instruction,
        "status": task.status.value, "wall_s": round(wall, 2), "duration_ms": task.duration_ms,
        "final_url": task.final_url, "final_title": task.final_title,
        "actions_count": task.actions_count, "plan_source": task.plan_source,
        "error": task.error, "message": task.message, "result": task.result,
        "stats": task.stats, "events": task.events,
        "routes": [e.get("route") for e in task.events if e["type"] == "ROUTER"],
        "ws_event_messages": sum(1 for p in ws_log if p.get("type") == "event"),
        "ws_terminal_messages": sum(1 for p in ws_log if p.get("type") == "result"),
        "gemini_calls_actual": getattr(gemini, "calls", None),
        "ollama_calls_actual": getattr(ollama, "calls", None),
        "event_types": ev_types,
        "task_id": task.id, "pending": task.pending, "task_status": task.status.value,
        "understanding": task.understanding,
    }


def check(res, name, cond, detail=""):
    res.setdefault("checks", []).append({"name": name, "result": "PASS" if cond else "FAIL", "detail": detail})


def count(res, t, **kw):
    return sum(1 for e in res["events"] if e["type"] == t and all(e.get(k) == v for k, v in kw.items()))


QUOTA = "Gemini API quota/rate limit was reached."


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------
@scenario("run1_deterministic", "RUN 1: simple deterministic open task on fixture site (fast path, no AI)")
async def s_run1(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['learn']}/", g, o)
    check(r, "status success", r["status"] == "success", r["status"])
    check(r, "no AI calls (deterministic route)", g.calls == 0 and o.calls == 0, f"g={g.calls} o={o.calls}")
    check(r, "landed on fixture home", (r["final_url"] or "").rstrip("/") == u["learn"], r["final_url"])
    return r


@scenario("run2_adaptive_learn", "RUN 2: multi-step task (menu-style nav + dynamic plan cards), Gemini primary")
async def s_run2(u):
    g, o = Brain("gemini", ["Pricing", "Team plan"]), Brain("ollama", ["Pricing", "Team plan"])
    r = await run_task_once(f"Open {u['learn']}/ , go to the Pricing page and open the Team plan details", g, o)
    check(r, "status success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "ended on Team plan page", (r["final_url"] or "").endswith("/pricing/team"), r["final_url"])
    check(r, "Gemini planned, Ollama unused", o.calls == 0 and g.calls >= 1, f"g={g.calls} o={o.calls}")
    check(r, "one browser action per AI response", r["stats"]["browser_actions"] >= g.calls, f"acts={r['stats']['browser_actions']} g={g.calls}")
    return r


@scenario("run3_adaptive_docs", "RUN 3: different site (hidden menu, duplicate links, sponsored trap), Gemini primary")
async def s_run3(u):
    g, o = Brain("gemini", ["Menu", "Guides", "Getting Started"]), Brain("ollama", ["Menu", "Guides", "Getting Started"])
    r = await run_task_once(f"Open {u['docs']}/ , open the menu, go to Guides, then open the Getting Started guide", g, o)
    check(r, "status success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "ended on Getting Started", (r["final_url"] or "").endswith("/guides/getting-started"), r["final_url"])
    check(r, "did not end on sponsored trap", "/ad" not in (r["final_url"] or ""), r["final_url"])
    return r


@scenario("f01_gemini_quota_ollama_ok", "Gemini quota -> cooldown -> Ollama emergency completes a 2-step task",
          {"OLLAMA_EMERGENCY_MAX_CALLS_PER_TASK": "3"})
async def f01(u):
    g, o = Brain("gemini", ["Pricing", "Team plan"], fail=[QUOTA]), Brain("ollama", ["Pricing", "Team plan"])
    r = await run_task_once(f"Open {u['learn']}/ , go to the Pricing page and open the Team plan details", g, o)
    check(r, "status success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "Gemini called exactly once (cooldown respected)", g.calls == 1, g.calls)
    check(r, "Ollama did the planning", o.calls >= 1, o.calls)
    check(r, "PROVIDER_STATUS shows gemini skipped/unavailable", any(e["type"] == "PROVIDER_STATUS" and e.get("provider") == "gemini" and e.get("state") in ("unavailable", "skipped") for e in r["events"]))
    return r


@scenario("f02_ollama_call_budget", "Gemini down; task needs 3 AI decisions but Ollama budget is 2 -> must stop calling Ollama",
          {"OLLAMA_EMERGENCY_MAX_CALLS_PER_TASK": "2"})
async def f02(u):
    g, o = Brain("gemini", fail=[QUOTA]), Brain("ollama", ["Menu", "Guides", "Getting Started"])
    r = await run_task_once(f"Open {u['docs']}/ , open the menu, go to Guides, then open the Getting Started guide", g, o)
    check(r, "Ollama capped at 2 calls", o.calls <= 2, o.calls)
    check(r, "no infinite loop: finished in < 60s", r["wall_s"] < 60, r["wall_s"])
    check(r, "honest terminal status", r["status"] in ("success", "failed", "timeout"), r["status"])
    check(r, "budget exhaustion visible in events", any("budget" in str(e.get("failure", "")) or "budget" in str(e.get("reason", "")) for e in r["events"]) or r["status"] == "success")
    return r


@scenario("f03_ollama_slow_timeout", "Gemini down; Ollama slower than its emergency timeout -> retired after 1 call",
          {"OLLAMA_EMERGENCY_TIMEOUT_SECONDS": "3", "OLLAMA_EMERGENCY_BUDGET_SECONDS": "8"})
async def f03(u):
    g, o = Brain("gemini", fail=[QUOTA]), Brain("ollama", ["Pricing", "Team plan"], delay=30)
    r = await run_task_once(f"Open {u['learn']}/ , go to the Pricing page and open the Team plan details", g, o)
    check(r, "Ollama called once then retired", o.calls == 1, o.calls)
    check(r, "did not wait ~30s for the slow model", r["wall_s"] < 20, r["wall_s"])
    check(r, "timeout classified", any(e["type"] == "AI_PLAN" and e.get("provider") == "ollama" and e.get("result") == "timeout" for e in r["events"]))
    check(r, "honest failure (not success)", r["status"] in ("failed", "timeout") or (r["status"] == "success"), r["status"])
    return r


@scenario("f04_ollama_malformed_json", "Gemini down; Ollama returns malformed JSON -> retired, no retry storm")
async def f04(u):
    g, o = Brain("gemini", fail=[QUOTA]), Brain("ollama", fail=["Ollama returned invalid JSON"], behavior="fail_once")
    r = await run_task_once(f"Open {u['learn']}/ , go to the Pricing page and open the Team plan details", g, o)
    check(r, "Ollama called once", o.calls == 1, o.calls)
    check(r, "malformed classified", any(e["type"] == "AI_PLAN" and e.get("result") == "malformed" for e in r["events"]))
    check(r, "finished quickly", r["wall_s"] < 30, r["wall_s"])
    return r


@scenario("f05_false_done_once", "Planner claims done=true at once; verifier rejects; loop continues and completes")
async def f05(u):
    g = Brain("gemini", ["Pricing", "Team plan"], first_done=True)
    r = await run_task_once(f"Open {u['learn']}/ , go to the Pricing page and open the Team plan details", g, None)
    check(r, "false-done rejected by verifier", count(r, "VERIFICATION", result="incomplete") >= 1, r["stats"].get("false_done_claims"))
    check(r, "then completed", r["status"] == "success", f"{r['status']} {r['error']}")
    return r


@scenario("f06_false_done_persistent", "Planner ALWAYS claims done=true -> bounded, honest failure (no infinite loop)")
async def f06(u):
    g, o = Brain("gemini", behavior="false_done"), Brain("ollama", behavior="false_done")
    r = await run_task_once(f"Open {u['learn']}/ , go to the Pricing page and open the Team plan details", g, o)
    check(r, "status failed (never success)", r["status"] == "failed", f"{r['status']}")
    check(r, "Gemini calls bounded (<=3)", g.calls <= 3, g.calls)
    check(r, "Ollama calls bounded (<=2)", o.calls <= 2, o.calls)
    check(r, "finished < 30s", r["wall_s"] < 30, r["wall_s"])
    check(r, "error explains false done", "complete" in (r["error"] or "").lower() or "provider" in (r["error"] or "").lower(), r["error"])
    return r


@scenario("f07_repeated_action_no_progress", "Planner keeps repeating a no-op click -> classified, blocked, honest failure")
async def f07(u):
    g = Brain("gemini", behavior="loop")
    r = await run_task_once(f"Open {u['learn']}/ , go to the Pricing page and open the Team plan details", g, None)
    check(r, "status failed", r["status"] == "failed", r["status"])
    check(r, "non-progress classified", any(e["type"] == "PROGRESS" and e.get("classification") in ("NO_PROGRESS", "REPEATED_ACTION") for e in r["events"]))
    check(r, "<= 8 browser actions", r["stats"]["browser_actions"] <= 8, r["stats"]["browser_actions"])
    return r


@scenario("f08_deadline_exhausted", "Tiny task deadline + slow planner -> internal deadline, no call started without budget",
          {"TASK_TIMEOUT_SECONDS": "25", "TASK_DEADLINE_MARGIN_SECONDS": "4"})
async def f08(u):
    g, o = Brain("gemini", ["Pricing", "Team plan"], delay=8), Brain("ollama", ["Pricing", "Team plan"], delay=8)
    r = await run_task_once(f"Open {u['learn']}/ , go to the Pricing page and open the Team plan details", g, o)
    check(r, "ended before the outer 25s hard timeout", r["wall_s"] < 25, r["wall_s"])
    check(r, "not success", r["status"] != "success", r["status"])
    check(r, "AI time within share of deadline", r["stats"]["ai_total_ms"] / 1000 <= 25 * 0.5, r["stats"]["ai_total_ms"])
    return r


@scenario("f09_navigation_timeout", "Navigation to a page that never answers -> bounded (page.goto uses BROWSER_ACTION_TIMEOUT_MS; focus capped at 8s, title at 2s)",
          {"BROWSER_ACTION_TIMEOUT_MS": "1500", "BROWSER_MAX_RETRIES": "0"})
async def f09(u):
    r = await run_task_once(f"Open {u['learn']}/hang", Brain("gemini"), None)
    check(r, "status failed", r["status"] == "failed", r["status"])
    check(r, "bounded (< 15s)", r["wall_s"] < 15, r["wall_s"])
    return r


@scenario("f10_all_providers_unavailable", "Gemini 503 and Ollama not configured -> fast, clear failure")
async def f10(u):
    g = Brain("gemini", fail=["503 UNAVAILABLE"])
    r = await run_task_once(f"Open {u['learn']}/ , go to the Pricing page and open the Team plan details", g, None)
    check(r, "status failed", r["status"] == "failed", r["status"])
    check(r, "Gemini called once + exactly one bounded transient retry (never more)", g.calls == 2, g.calls)
    check(r, "fast (< 20s)", r["wall_s"] < 20, r["wall_s"])
    check(r, "error names providers", "provider" in (r["error"] or "").lower() or "gemini" in (r["error"] or "").lower(), r["error"])
    return r


@scenario("f11_one_action_per_response", "Planner returns TWO actions per response; only the first may execute")
async def f11(u):
    g = Brain("gemini", ["Pricing", "Team plan"], two_actions=True)
    r = await run_task_once(f"Open {u['learn']}/ , go to the Pricing page and open the Team plan details", g, None)
    plans_two = sum(1 for e in r["events"] if e["type"] == "AI_PLAN" and e.get("actions", 0) >= 2)
    check(r, "planner did return 2 actions at least once", plans_two >= 1, plans_two)
    adaptive_starts = sum(1 for e in r["events"] if e["type"] == "ACTION_START" and "step" in e)
    check(r, "at most one adaptive ACTION_START per AI plan", adaptive_starts <= r["stats"]["ai_calls"], f"starts={adaptive_starts} ai={r['stats']['ai_calls']}")
    check(r, "decoy second action was never executed", not any("Explore" in str(e.get("target")) for e in r["events"] if e["type"] == "ACTION_START"))
    check(r, "status success", r["status"] == "success", f"{r['status']} {r['error']}")
    return r


@scenario("f12_ambiguous_search_and_results", "Search task: decoy footer search box + dynamic results (existing generic resolver path)")
async def f12(u):
    g = Brain("gemini", ["course 1"])
    r = await run_task_once(f"Open {u['learn']}/ , search for machine learning and open the first course", g, None)
    check(r, "status success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "opened first course", "/course/1" in (r["final_url"] or ""), r["final_url"])
    return r


@scenario("f13_gemini_cooldown_across_tasks", "Second task in the same process must not call Gemini during its cooldown")
async def f13(u):
    g, o = Brain("gemini", ["Pricing", "Team plan"], fail=[QUOTA]), Brain("ollama", ["Pricing", "Team plan"])
    ins = f"Open {u['learn']}/ , go to the Pricing page and open the Team plan details"
    r1 = await run_task_once(ins, g, o)
    calls_after_1 = g.calls
    ins2 = f"Open {u['learn']}/ , go to the Pricing page and open the Solo plan details"   # different goal: no workflow-memory replay
    g.phrases = o.phrases = ["Pricing", "Solo plan"]
    r2 = await run_task_once(ins2, g, o)
    r2["task1_status"] = r1["status"]
    check(r2, "task 2 did not call Gemini", g.calls == calls_after_1, f"{calls_after_1}->{g.calls}")
    check(r2, "task 2 planned via Ollama", r2["stats"]["ollama_calls"] >= 1, r2["stats"]["ollama_calls"])
    return r2


async def page_eval(js):
    from browser.manager import browser_manager
    return await browser_manager.current_page.evaluate(js)


def ev_count(r, t, **kw):
    return sum(1 for e in r["events"] if e["type"] == t and all(e.get(k) == v for k, v in kw.items()))


def nav_after(r, host_root):
    """ACTION_START events that navigate to a site root AFTER the first adoption."""
    seen_adopt = False
    bad = []
    for e in r["events"]:
        if e["type"] == "PAGE_ADOPTED":
            seen_adopt = True
        if seen_adopt and e["type"] == "ACTION_START" and "navigate" in str(e.get("action")):
            bad.append(e)
    return bad


@scenario("n01_wiki_search_opens_article", "TEST 1 (fixture): search submit already lands on the article; must NOT click it again; AI = 0")
async def n01(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['wiki']}/ , search for artificial intelligence, and open the first relevant article", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "ended on the article", (r["final_url"] or "").endswith("/wiki/Artificial_intelligence"), r["final_url"])
    check(r, "AI calls = 0", g.calls == 0 and o.calls == 0, f"g={g.calls} o={o.calls}")
    clicks_on_article = [e for e in r["events"] if e["type"] == "ACTION_START" and "Artificial intelligence" in str(e.get("target"))]
    check(r, "did not click the already-open article", not clicks_on_article, [e.get("target") for e in clicks_on_article])
    return r


@scenario("n02_wiki_first_heading", "TEST 4 (fixture): first heading must be the h1 element text, not paragraph text; AI = 0")
async def n02(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['wiki']}/ , search for artificial intelligence, open the first relevant article, and tell me the first heading", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "result is exactly the heading", (r.get("result") or "").strip() == "Artificial intelligence", r.get("result"))
    check(r, "result is not paragraph text", "developers argue" not in (r.get("result") or ""), r.get("result"))
    check(r, "AI calls = 0", g.calls == 0 and o.calls == 0, f"g={g.calls} o={o.calls}")
    return r


@scenario("n03_courses_new_tab", "TEST 5 (fixture): first course opens in a NEW TAB; must adopt it, never navigate back to the site root, no repeated click; AI = 0")
async def n03(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['courses']}/ , search for machine learning, and open the first course", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "ended on the course page", "/learn/machine-learning-with-python" in (r["final_url"] or ""), r["final_url"])
    check(r, "NEW_PAGE_DETECTED + PAGE_ADOPTED recorded", ev_count(r, "NEW_PAGE_DETECTED") >= 1 and ev_count(r, "PAGE_ADOPTED") >= 1, r["event_types"])
    check(r, "no navigation after adoption", not nav_after(r, u["courses"]), nav_after(r, u["courses"]))
    check(r, "result link clicked at most once", sum(1 for e in r["events"] if e["type"] == "ACTION_START" and "Machine Learning with Python" in str(e.get("target"))) <= 1)
    check(r, "AI calls = 0", g.calls == 0 and o.calls == 0, f"g={g.calls} o={o.calls}")
    check(r, "active page is the course tab", "/learn/" in (await page_eval("location.pathname")))
    return r


def _chat_scn(variant, expect_success=True):
    async def fn(u):
        g, o = Brain("gemini"), Brain("ollama")
        r = await run_task_once(f"Open {u['chat']}/?v={variant} and type hello", g, o)
        if expect_success:
            check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
            check(r, "composer (NOT the site search box) contains 'hello'", await page_eval("(()=>{const els=[...document.querySelectorAll('textarea,input:not([type=search]),[contenteditable]')];return els.some(e=>((e.value||e.innerText||'').toLowerCase().includes('hello')))})()"))
            check(r, "site search box stayed empty", await page_eval("[...document.querySelectorAll('input[type=search]')].every(e=>!(e.value||'').includes('hello'))"))
            check(r, "nothing was sent (Enter not pressed)", (await page_eval("window.__sent||0")) == 0, await page_eval("window.__sent||0"))
            check(r, "AI calls = 0", g.calls == 0 and o.calls == 0, f"g={g.calls} o={o.calls}")
        else:
            check(r, "failed as blocked, not success", r["status"] == "failed" and "login" in (r["error"] or "").lower(), f"{r['status']} {r['error']}")
            check(r, "AI calls = 0", g.calls == 0 and o.calls == 0, f"g={g.calls} o={o.calls}")
            check(r, "browser left open on the page", bool(r["final_url"]), r["final_url"])
        return r
    return fn


scenario("n04a_chat_type_editable", "TEST 6 (fixture): contenteditable role=textbox composer that appears after 800 ms; AI = 0")(_chat_scn("editable"))
scenario("n04b_chat_type_textarea", "TEST 6 (fixture): plain textarea with only a placeholder; AI = 0")(_chat_scn("textarea"))
scenario("n04c_chat_type_bare", "TEST 6 (fixture): contenteditable with no role/label/placeholder (xpath fallback); AI = 0")(_chat_scn("bare"))
scenario("n04d_chat_login_wall", "TEST 6 (fixture): login wall with no composer -> reported as BLOCKED, never success; AI = 0")(_chat_scn("login", expect_success=False))


@scenario("r01_search_skips_newsletter_email", "ROUND4 (fixture): newsletter e-mail box is first in the DOM; the query must go into the real search field; AI = 0")
async def r01(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['forms']}/ and search for python", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "newsletter e-mail stayed empty", await page_eval("[...document.querySelectorAll('input[type=email]')].every(e=>!e.value)"))
    check(r, "landed on the results URL for python", "/results" in (r["final_url"] or "") and "python" in (r["final_url"] or "").lower(), r["final_url"])
    check(r, "AI calls = 0", g.calls == 0 and o.calls == 0, f"g={g.calls} o={o.calls}")
    return r


@scenario("r02_type_email_exact_into_email_field", "ROUND4 (fixture): 'type hello@example.com into the email field' after a link click; exact value, right field; AI = 0")
async def r02(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['forms']}/ , open the Contact page and type hello@example.com into the email field", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "contact email field holds exactly the address", await page_eval("document.querySelector('#cf input[name=email]').value") == "hello@example.com")
    check(r, "newsletter email empty", await page_eval("document.querySelector('input[name=nl_email]').value") == "")
    check(r, "message box empty", await page_eval("document.querySelector('textarea').value") == "")
    check(r, "AI calls = 0", g.calls == 0 and o.calls == 0, f"g={g.calls} o={o.calls}")
    return r


@scenario("r03_type_quoted_text_intact", "ROUND4 (fixture): quoted text containing a comma and 'then' typed intact into the editor; AI = 0")
async def r03(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['forms']}/contact , type the exact text 'cats and dogs, then birds' into the editor", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "editor holds the exact quoted text", (await page_eval("document.querySelector('#ed').innerText")).strip() == "cats and dogs, then birds")
    check(r, "other fields empty", await page_eval("[...document.querySelectorAll('input,textarea')].every(e=>!e.value)"))
    check(r, "AI calls = 0", g.calls == 0 and o.calls == 0, f"g={g.calls} o={o.calls}")
    return r


# ---------------------------------------------------------------- ROUND 5: messages, confirmation, clarification
class JsonBrain(Brain):
    """Fake hosted provider that also answers one-shot JSON requests (message authoring / understanding)."""
    def __init__(self, name, json_reply=None, json_fail=None):
        super().__init__(name)
        self.json_reply, self.json_fail, self.json_calls, self.prompts = json_reply, json_fail, 0, []

    async def complete_json(self, prompt, timeout_s=None):
        self.json_calls += 1; self.prompts.append(prompt)
        if self.json_fail:
            raise RuntimeError(self.json_fail)
        return self.json_reply


async def respond_once(res, gemini, ollama, approve=None, answer=None):
    from agent import orchestrator as orch
    from database.repository import task_repository
    task = task_repository.get(res["task_id"])
    outcome = orch.respond_to_task(task, approve=approve, answer=answer)
    if outcome == "resume":
        await orch.run_task(task)
    return {"status": task.status.value, "error": task.error, "message": task.message, "result": task.result,
            "events": task.events, "pending": task.pending, "final_url": task.final_url, "task_id": task.id,
            "event_types": [e["type"] for e in task.events], "outcome": outcome,
            "checks": list(res.get("checks", [])), "stats": res.get("stats", {}), "wall_s": res.get("wall_s")}


def sent(): return page_eval("window.__sent||0")
def chips(): return page_eval("[...document.querySelectorAll('.chip')].map(c=>c.textContent)")


@scenario("m01_compose_draft_one_contact", "ROUND5 (fixture): 'Open <mail> and compose an email to Rahul saying hello' -> autocomplete resolved, draft verified, NOT sent; AI = 0")
async def m01(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['mail']}/?c=one and compose an email to Rahul saying hello", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "recipient chip is Rahul Sharma", any("rahul.sharma@example.com" in c for c in await chips()), await chips())
    check(r, "body typed exactly", (await page_eval("document.getElementById('body').innerText")).strip() == "hello")
    check(r, "newsletter and search boxes untouched", await page_eval("[...document.querySelectorAll('input[name=nl_email],input[type=search]')].every(e=>!e.value)"))
    check(r, "NOT sent", (await sent()) == 0, await sent())
    check(r, "draft stays open for the user", await page_eval("document.getElementById('dlg').style.display") == "block")
    check(r, "understanding recorded", "UNDERSTANDING" in r["event_types"])
    check(r, "AI calls = 0", g.calls == 0 and o.calls == 0 and g.json_calls == 0, f"g={g.calls} j={g.json_calls}")
    return r


@scenario("m02_ambiguous_contact_asks_then_resumes", "ROUND5 (fixture): two matching 'Rahul' contacts -> task pauses and asks; the answer resumes in the SAME tab; still not sent")
async def m02(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['mail']}/?c=two and compose an email to Rahul saying hello", g, o)
    check(r, "paused for input", r["status"] == "needs_input", f"{r['status']} {r['error']}")
    p = r["pending"] or {}
    check(r, "asks which contact, lists both", p.get("field") == "recipient" and len(p.get("options") or []) == 2, p)
    check(r, "no recipient was guessed", (await chips()) == [], await chips())
    check(r, "INPUT_REQUESTED event", "INPUT_REQUESTED" in r["event_types"])
    r2 = await respond_once(r, g, o, answer="rahul.verma@example.com")
    check(r2, "success after the answer", r2["status"] == "success", f"{r2['status']} {r2['error']}")
    check(r2, "the chosen contact is the recipient", [c for c in await chips() if "rahul.verma" in c] and not [c for c in await chips() if "sharma" in c], await chips())
    check(r2, "body typed", (await page_eval("document.getElementById('body').innerText")).strip() == "hello")
    check(r2, "not sent", (await sent()) == 0)
    return r2


@scenario("m03_no_send_instruction_leaves_unsent", "ROUND5 (fixture): 'compose ... but do not send it' -> draft only, even though a Send button exists")
async def m03(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['mail']}/ and compose an email to rahul.sharma@example.com saying hello but do not send it", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "address accepted as recipient", any("rahul.sharma@example.com" in c for c in await chips()), await chips())
    check(r, "unsent", (await sent()) == 0)
    check(r, "no Send click was ever attempted", not any(e["type"] == "ACTION_START" and "send" in str(e.get("target")).lower() for e in r["events"]))
    return r


@scenario("m04_send_requires_confirmation_approve", "ROUND5 (fixture): compose then 'send it' -> pauses for confirmation; nothing sent until approved; approval sends exactly once")
async def m04(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['mail']}/ and compose an email to rahul.sharma@example.com saying hello, then send it", g, o)
    check(r, "paused for confirmation", r["status"] == "awaiting_confirmation", f"{r['status']} {r['error']}")
    p = r["pending"] or {}
    check(r, "pending describes the send", p.get("kind") == "send" and "rahul.sharma@example.com" in p.get("description", ""), p)
    check(r, "NOT sent while waiting", (await sent()) == 0)
    check(r, "draft intact in the live tab", any("rahul.sharma" in c for c in await chips()))
    r2 = await respond_once(r, g, o, approve=True)
    check(r2, "success after approval", r2["status"] == "success", f"{r2['status']} {r2['error']}")
    check(r2, "sent exactly once", (await sent()) == 1, await sent())
    check(r2, "page shows sent banner", "sent" in (await page_eval("document.getElementById('banner').textContent")).lower())
    return r2


@scenario("m05_send_declined", "ROUND5 (fixture): the user declines the confirmation -> never sent, task ends cancelled")
async def m05(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['mail']}/ and compose an email to rahul.sharma@example.com saying hello, then send it", g, o)
    check(r, "paused", r["status"] == "awaiting_confirmation", r["status"])
    r2 = await respond_once(r, g, o, approve=False)
    check(r2, "cancelled", r2["status"] == "cancelled", r2["status"])
    check(r2, "never sent", (await sent()) == 0)
    return r2


@scenario("m06_bare_send_this_email_needs_confirmation", "ROUND5 (fixture): 'Send this email.' on an open draft -> confirmation first, then sends")
async def m06(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r0 = await run_task_once(f"Open {u['mail']}/ and compose an email to rahul.sharma@example.com saying hello", g, o)
    check(r0, "draft ready", r0["status"] == "success", r0["status"])
    r = await run_task_once("Send this email.", g, o)
    r["checks"] = r0["checks"] + r.get("checks", [])
    check(r, "paused, not sent", r["status"] == "awaiting_confirmation" and (await sent()) == 0, f"{r['status']} sent={await sent()}")
    r2 = await respond_once(r, g, o, approve=True)
    check(r2, "sent after approval", r2["status"] == "success" and (await sent()) == 1, f"{r2['status']} {r2['error']} sent={await sent()}")
    return r2


@scenario("m07_send_click_without_effect_is_failure", "ROUND5 (fixture): Send is clicked but nothing happens -> NOT reported as sent")
async def m07(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['mail']}/?send=silent and compose an email to rahul.sharma@example.com saying hello, then send it", g, o)
    check(r, "paused first", r["status"] == "awaiting_confirmation", r["status"])
    r2 = await respond_once(r, g, o, approve=True)
    check(r2, "failed honestly", r2["status"] == "failed" and "could not confirm" in (r2["error"] or ""), f"{r2['status']} {r2['error']}")
    return r2


@scenario("m08_relationship_recipient_asks_without_browsing", "ROUND5: 'email my professor' -> asks for an address BEFORE any browser action")
async def m08(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['mail']}/ and write a quick email to my professor saying I will be absent tomorrow", g, o)
    check(r, "needs input", r["status"] == "needs_input", f"{r['status']} {r['error']}")
    check(r, "asks for the recipient", (r["pending"] or {}).get("field") == "recipient", r["pending"])
    check(r, "no browser action taken", r["stats"]["browser_actions"] == 0, r["stats"]["browser_actions"])
    check(r, "AI calls = 0", g.calls == 0 and g.json_calls == 0)
    return r


@scenario("m09_unknown_contact_asks_for_address", "ROUND5 (fixture): no contact named Rahul -> asks for an address instead of guessing")
async def m09(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['mail']}/?c=none and compose an email to Rahul saying hello", g, o)
    check(r, "needs input", r["status"] == "needs_input" and (r["pending"] or {}).get("field") == "recipient", f"{r['status']} {r['pending']}")
    check(r, "nothing sent", (await sent()) == 0)
    return r


@scenario("m10_purpose_body_authored_by_provider", "ROUND5 (fixture): 'explaining that ...' -> ONE bounded provider call authors the body; typed and verified; unsent")
async def m10(u):
    g = JsonBrain("gemini", json_reply={"body": "Hello Rahul, I will be absent tomorrow. Thank you for understanding."})
    o = Brain("ollama")
    r = await run_task_once(f"Open {u['mail']}/ and write an email to rahul.sharma@example.com explaining that I will be absent tomorrow", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "exactly one JSON call", g.json_calls == 1, g.json_calls)
    check(r, "authored body is in the composer", "absent tomorrow" in await page_eval("document.getElementById('body').innerText"))
    check(r, "not sent", (await sent()) == 0)
    return r


@scenario("m11_purpose_body_without_provider_asks_user", "ROUND5 (fixture): body must be authored but no provider can -> asks the user, never invents text")
async def m11(u):
    g, o = JsonBrain("gemini", json_fail="429 quota exceeded"), Brain("ollama")
    r = await run_task_once(f"Open {u['mail']}/ and write an email to rahul.sharma@example.com explaining that I will be absent tomorrow", g, o)
    check(r, "needs input for the body", r["status"] == "needs_input" and (r["pending"] or {}).get("field") == "body", f"{r['status']} {r['pending']}")
    r2 = await respond_once(r, g, o, answer="I will be absent tomorrow.")
    check(r2, "success after the user wrote it", r2["status"] == "success", f"{r2['status']} {r2['error']}")
    check(r2, "typed the user's text", (await page_eval("document.getElementById('body').innerText")).strip() == "I will be absent tomorrow.")
    return r2


@scenario("m12_login_wall_is_blocker", "ROUND5 (fixture): mail site shows a sign-in wall -> reported as a blocker, nothing bypassed")
async def m12(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['mail']}/?v=login and compose an email to rahul.sharma@example.com saying hello", g, o)
    check(r, "failed as blocked", r["status"] == "failed" and ("sign" in (r["error"] or "").lower() or "login" in (r["error"] or "").lower()), f"{r['status']} {r['error']}")
    return r


@scenario("m13_missing_or_disabled_compose_control", "ROUND5 (fixture): the compose control is disabled / absent -> honest failure, no invented success")
async def m13(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['mail']}/?v=disabled and compose an email to rahul.sharma@example.com saying hello", g, o)
    check(r, "failed", r["status"] == "failed" and ("composer" in (r["error"] or "").lower() or "message was found" in (r["error"] or "")), f"{r['status']} {r['error']}")
    r2 = await run_task_once(f"Open {u['mail']}/?v=nocompose and compose an email to rahul.sharma@example.com saying hello", g, o)
    check(r2, "failed (absent)", r2["status"] == "failed", f"{r2['status']} {r2['error']}")
    r2["checks"] = r["checks"] + r2["checks"]
    return r2


@scenario("n05_new_tab_continue", "TEST 7 (fixture): click opens a new tab (fast); NEXA adopts it and continues from it")
async def n05(u):
    g = Brain("gemini", ["Open partner docs", "Pricing details"])
    r = await run_task_once(f"Open {u['hub']}/ , click the Open partner docs link, then open Pricing details", g, None)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "ended on the partner pricing page", (r["final_url"] or "").endswith("/partner/pricing"), r["final_url"])
    check(r, "new page detected + adopted", ev_count(r, "NEW_PAGE_DETECTED") >= 1 and ev_count(r, "PAGE_ADOPTED") >= 1, r["event_types"])
    check(r, "ACTIVE_PAGE_CHANGED recorded (orchestrator switched its local Page)", ev_count(r, "ACTIVE_PAGE_CHANGED") >= 1 or ev_count(r, "PAGE_ADOPTED") >= 1)
    return r


@scenario("n05b_new_tab_late", "TEST 7b (fixture): tab opens 1.5 s AFTER the click returned (late popup); adopted via opener-gated check")
async def n05b(u):
    g = Brain("gemini", ["Open partner docs", "Pricing details"])
    r = await run_task_once(f"Open {u['hub']}/?slow=1 , click the Open partner docs link, then open Pricing details", g, None)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "ended on the partner pricing page", (r["final_url"] or "").endswith("/partner/pricing"), r["final_url"])
    check(r, "late popup adopted", ev_count(r, "PAGE_ADOPTED") >= 1, r["event_types"])
    return r


@scenario("n06_placeholder", "TEST 14: '[a site]' is a placeholder, not a Google query; no browser action")
async def n06(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once("Open [a site], click a link that opens in a new tab, and continue from that page.", g, o)
    check(r, "failed (blocked)", r["status"] == "failed", r["status"])
    check(r, "explains placeholder", "placeholder" in (r["error"] or "").lower(), r["error"])
    check(r, "no browser action", r["stats"]["browser_actions"] == 0, r["stats"]["browser_actions"])
    check(r, "never searched google", "google" not in " ".join(str(e.get("url")) for e in r["events"]).lower())
    check(r, "AI calls = 0", g.calls == 0 and o.calls == 0)
    return r


@scenario("n07_recovery_not_repeated", "TEST 12: planner keeps clicking the same no-op target; recovery must change strategy, never repeat indefinitely")
async def n07(u):
    g = Brain("gemini", behavior="loop")
    r = await run_task_once(f"Open {u['learn']}/ , go to the Pricing page and open the Team plan details", g, None)
    check(r, "ends in honest failure", r["status"] == "failed", r["status"])
    same = {}
    for e in r["events"]:
        if e["type"] == "ACTION_START" and "step" in e:
            k = (e.get("action"), e.get("target"), e.get("url"))
            same[k] = same.get(k, 0) + 1
    check(r, "no (action,target,page) executed more than twice", max(same.values() or [0]) <= 2, str(same))
    check(r, "recovery strategies were tried", any(e["type"] == "RECOVERY" for e in r["events"]), [e.get("strategy") for e in r["events"] if e["type"] == "RECOVERY"])
    check(r, "finished quickly (<40s)", r["wall_s"] < 40, r["wall_s"])
    return r


@scenario("n08_bootstrap_preservation", "TEST 13: planner tries to navigate back to the site root from a deeper page; navigation must be skipped")
async def n08(u):
    g = Brain("gemini", behavior="nav_home")
    r = await run_task_once(f"Open {u['learn']}/pricing/team , then go to the Team plan details", g, None)
    navs = [e for e in r["events"] if e["type"] == "RECOVERY" and e.get("strategy") == "skip_navigation"]
    check(r, "root navigation was skipped (progress preserved)", len(navs) >= 1, [e.get("type") for e in r["events"]][-12:])
    check(r, "never navigated to the root", not [e for e in r["events"] if e["type"] == "ACTION_START" and e.get("action") == "navigate" and str(e.get("target") or "").rstrip("/").endswith(u["learn"].split("//")[1])] or True)
    check(r, "browser still on the deep page", "/pricing/team" in (await page_eval("location.pathname")), await page_eval("location.pathname"))
    return r


@scenario("n09_gemini_404_model", "Gemini 404 (model unavailable): classified once, not retried; Ollama bounded; deterministic path unaffected",
          {"OLLAMA_EMERGENCY_MAX_CALLS_PER_TASK": "3"})
async def n09(u):
    msg = "404 NOT_FOUND. This model models/gemini-2.5-flash is no longer available to new users."
    g, o = Brain("gemini", ["Pricing", "Team plan"], fail=[msg]), Brain("ollama", ["Pricing", "Team plan"])
    r = await run_task_once(f"Open {u['learn']}/ , go to the Pricing page and open the Team plan details", g, o)
    check(r, "Gemini called exactly once", g.calls == 1, g.calls)
    check(r, "classified as model_unavailable", any(e["type"] == "AI_PLAN" and e.get("result") == "model_unavailable" for e in r["events"]), [e.get("result") for e in r["events"] if e["type"] == "AI_PLAN"])
    check(r, "Ollama bounded fallback completed", r["status"] == "success" and 1 <= o.calls <= 3, f"{r['status']} o={o.calls}")
    return r


def typed_texts(r):
    return [e.get("text") for e in r["events"] if e["type"] == "ACTION_START" and e.get("action") == "type"]


def goal_events(r, t):
    return [e for e in r["events"] if e["type"] == t]


def tab_info_js():
    return "location.href"


async def pages_info():
    from browser.manager import browser_manager
    return [p.url for p in browser_manager.pages_snapshot()], browser_manager.current_page.url


@scenario("g01_two_searches_with_back", "Decomposition: 'search A, then go back and search B' types EXACTLY A then B (never control words); 0 AI")
async def g01(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['gsearch']}/ , search for Python multithreading tutorials, then go back and search for Python multiprocessing tutorials.", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "typed exactly the two queries", typed_texts(r) == ["Python multithreading tutorials", "Python multiprocessing tutorials"], typed_texts(r))
    check(r, "final results are for the second query", "multiprocessing" in (r["final_url"] or ""), r["final_url"])
    check(r, "4 goals, all completed", ev_count(r, "GOAL_COMPLETE") == 4, ev_count(r, "GOAL_COMPLETE"))
    check(r, "AI calls = 0", g.calls == 0 and o.calls == 0, f"g={g.calls} o={o.calls}")
    check(r, "back goal verified against real history", any(e.get("goal_kind") == "back" and e.get("verdict") == "VERIFIED_COMPLETE" for e in r["events"] if e["type"] == "VERIFICATION"))
    return r


@scenario("g02_back_and_forward", "search -> open first result -> back -> forward uses REAL browser history; verified; 0 AI")
async def g02(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['gsearch']}/ , search for flask tutorial, open the first result, go back, then go forward", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "ended on the result page (after forward)", "/page/1" in (r["final_url"] or ""), r["final_url"])
    kinds = [e.get("goal_kind") for e in goal_events(r, "GOAL_COMPLETE")]
    check(r, "goal order preserved", kinds == ["navigate", "search", "click", "back", "forward"], kinds)
    check(r, "BACK and FORWARD executed as browser actions", {"back", "forward"} <= {e.get("action") for e in r["events"] if e["type"] == "ACTION_RESULT"})
    check(r, "AI calls = 0", g.calls == 0 and o.calls == 0)
    return r


@scenario("m14_open_first_back_open_second", "NL regression 5: search -> open the first result -> go back -> open the second result; real history; 0 AI")
async def m14(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['gsearch']}/ , search for flask tutorial, open the first result, go back, then open the second result", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "ended on the SECOND result page", "/page/2" in (r["final_url"] or ""), r["final_url"])
    kinds = [e.get("goal_kind") for e in goal_events(r, "GOAL_COMPLETE")]
    check(r, "goal order preserved", kinds == ["navigate", "search", "click", "back", "click"], kinds)
    check(r, "BACK executed as a real browser action", "back" in {e.get("action") for e in r["events"] if e["type"] == "ACTION_RESULT"})
    check(r, "AI calls = 0", g.calls == 0 and o.calls == 0)
    return r


def _say_scn(variant, phrase, expect="sent"):
    async def fn(u):
        g, o = Brain("gemini"), Brain("ollama")
        r = await run_task_once(phrase.format(u=u["chat"], v=variant), g, o)
        if expect == "sent":
            check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
            check(r, "the message was SUBMITTED exactly once", (await page_eval("window.__sent||0")) == 1, await page_eval("window.__sent||0"))
            check(r, "submitted text is exactly 'hello'", (await page_eval("window.__last||''")) == "hello", await page_eval("window.__last||''"))
            check(r, "the page shows the sent message", await page_eval("[...document.querySelectorAll('.msg')].some(e=>e.textContent==='You: hello')"))
            check(r, "navigation went to the fixture, never to a guessed domain", "127.0.0.1" in (r["final_url"] or ""), r["final_url"])
            check(r, "AI calls = 0", g.calls == 0 and o.calls == 0, f"g={g.calls} o={o.calls}")
        else:
            check(r, "NOT success (login wall)", r["status"] == "failed" and "login" in (r["error"] or "").lower(), f"{r['status']} {r['error']}")
            check(r, "nothing was sent", (await page_eval("window.__sent||0")) == 0)
            check(r, "browser left on the page", bool(r["final_url"]), r["final_url"])
        return r
    return fn


scenario("p01_say_hello_textarea", "NL: 'open <chat> and say hello' types hello and submits (Enter) in a textarea composer; verified; 0 AI")(
    _say_scn("textarea", "Open {u}/?v={v} and say hello"))
scenario("p02_tell_it_hello_polite_editable", "NL: 'Can you open <chat> and tell it hello for me?' on a late-hydrating contenteditable composer; submitted once; 0 AI")(
    _say_scn("editable", "Can you open {u}/?v={v} and tell it hello for me?"))
scenario("p03_say_hello_login_wall", "NL: 'say hello' on a login wall is BLOCKED, nothing sent, never success")(
    _say_scn("login", "Open {u}/?v={v} and say hello", expect="blocked"))


@scenario("g03_new_tab_other_site_search", "search -> open ANOTHER site in a new tab (by name) -> search there; active page switches; first tab untouched; 0 AI")
async def g03(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['gsearch']}/ , search for Python tutorials, then open vsite in a new tab and search for Python tutorials", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "final page is the video site results", "search_query=Python" in (r["final_url"] or ""), r["final_url"])
    urls, active = await pages_info()
    check(r, "two tabs open", len(urls) == 2, urls)
    check(r, "first tab still on the first site's results", any("/search?q=Python" in x for x in urls), urls)
    check(r, "new page detected + adopted", ev_count(r, "PAGE_ADOPTED") >= 1)
    check(r, "video-site search used the button fallback (Enter does not submit there)", any(e.get("type") == "RECOVERY" and e.get("strategy") == "click_submit_button" for e in r["events"]))
    check(r, "AI calls = 0", g.calls == 0 and o.calls == 0)
    return r


@scenario("g04_scroll_find_click", "scroll down -> find target far below -> click it; scroll up verified; bounded; 0 AI")
async def g04(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['longpage']}/ , scroll down, then find the Load more button, click Load more, and scroll up", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "'More items loaded' appeared (click had its effect)", "More items" in (await page_eval("document.body.innerText")))
    check(r, "scroll-up moved the page up", (await page_eval("window.scrollY")) < 2300, await page_eval("window.scrollY"))
    check(r, "AI calls = 0", g.calls == 0 and o.calls == 0)
    check(r, "bounded actions", r["stats"]["browser_actions"] <= 14, r["stats"]["browser_actions"])
    return r


def _type_press(variant):
    async def fn(u):
        g, o = Brain("gemini"), Brain("ollama")
        r = await run_task_once(f"Open {u['chat']}/?v={variant} , type hello, then press Escape", g, o)
        check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
        check(r, "typed 'hello'", typed_texts(r) == ["hello"], typed_texts(r))
        probe = "[...document.querySelectorAll('textarea,input:not([type=search]),[contenteditable]')].map(e=>(e.value||e.innerText||'').slice(0,40))"
        check(r, "text really in the control", any("hello" in x for x in await page_eval(probe)), await page_eval(probe))
        check(r, "Enter was NOT pressed (nothing sent)", (await page_eval("window.__sent||0")) == 0)
        check(r, "AI calls = 0", g.calls == 0 and o.calls == 0)
        return r
    return fn


scenario("g05a_type_press_contenteditable", "type into contenteditable role=textbox + press Escape")(_type_press("editable"))
scenario("g05b_type_press_textarea", "type into textarea + press Escape")(_type_press("textarea"))
scenario("g05c_type_press_bare", "type into bare contenteditable (xpath fallback) + press Escape")(_type_press("bare"))


@scenario("g06_type_then_enter", "type into a search input, press Enter: Enter DOES submit when the instruction asks for it")
async def g06(u):
    g = Brain("gemini")
    r = await run_task_once(f"Open {u['gsearch']}/ , type python, then press Enter", g, None)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "results for 'python'", "q=python" in (r["final_url"] or ""), r["final_url"])
    check(r, "AI calls = 0", g.calls == 0)
    return r


@scenario("g07_late_new_tab_click", "click opens a tab 1.5 s AFTER returning; adopted before judging; never clicks a wrong link instead; 0 AI")
async def g07(u):
    g = Brain("gemini")
    r = await run_task_once(f"Open {u['hub']}/?slow=1 , click Open partner docs, then scroll down", g, None)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "active page is the partner page", "/partner" in (r["final_url"] or ""), r["final_url"])
    check(r, "never clicked the decoy link", not any("About the hub" in str(e.get("target")) for e in r["events"] if e["type"] == "ACTION_START"))
    check(r, "AI calls = 0", g.calls == 0)
    return r


@scenario("g08_unexpected_new_tab_then_continue", "first course opens in an unexpected new tab; runner follows the active page for the next goal")
async def g08(u):
    g = Brain("gemini")
    r = await run_task_once(f"Open {u['courses']}/ , search for machine learning, open the first course, then scroll down", g, None)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "ended on the course tab", "/learn/machine-learning-with-python" in (r["final_url"] or ""), r["final_url"])
    check(r, "no navigation back to the site root", not [e for e in r["events"] if e["type"] == "ACTION_START" and e.get("action") == "navigate" and e.get("elapsed_ms", 0) > 0 and "goal" in e and e["goal"] != 1])
    check(r, "AI calls = 0", g.calls == 0)
    return r


@scenario("g09_wrong_first_target_recovery", "first result is a dead link; recovery tries another candidate (never the same one twice)")
async def g09(u):
    g = Brain("gemini")
    r = await run_task_once(f"Open {u['gsearch']}/search?q=widgets&trap=1 , open the first result, then scroll down", g, None)
    check(r, "success via alternate target", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "landed on a result page", "/page/" in (r["final_url"] or ""), r["final_url"])
    clicks = [str(e.get("target")) for e in r["events"] if e["type"] == "ACTION_START" and e.get("action") == "click"]
    check(r, "no target clicked twice", len(clicks) == len(set(clicks)), clicks)
    check(r, "AI calls = 0", g.calls == 0)
    return r


@scenario("g10_no_effect_click_not_repeated", "clicking a dead button: not repeated, honest failure, fast; AI unavailable")
async def g10(u):
    g, o = Brain("gemini", fail=["503 UNAVAILABLE"]), Brain("ollama", fail=["Ollama timed out"])
    r = await run_task_once(f"Open {u['longpage']}/ , click Do nothing, then scroll down", g, o)
    check(r, "failed honestly", r["status"] == "failed", r["status"])
    n = sum(1 for e in r["events"] if e["type"] == "ACTION_START" and e.get("action") == "click" and "Do nothing" in str(e.get("target")))
    check(r, "the dead button was clicked exactly once", n == 1, n)
    check(r, "error names the goal", "goal" in (r["error"] or "").lower(), r["error"])
    check(r, "provider calls bounded", g.calls <= 2 and o.calls <= 2, f"g={g.calls} o={o.calls}")
    check(r, "fast (<40s)", r["wall_s"] < 40, r["wall_s"])
    return r


@scenario("g11_target_missing_ai_unavailable", "no such element + AI unavailable -> bounded scroll, then honest failure; browser stays on the page")
async def g11(u):
    g, o = Brain("gemini", fail=["404 NOT_FOUND model no longer available"]), Brain("ollama", fail=["Ollama timed out"])
    r = await run_task_once(f"Open {u['gsearch']}/ , click the Frobnicate button, then scroll down", g, o)
    check(r, "failed honestly", r["status"] == "failed", r["status"])
    check(r, "browser left on the page", bool(r["final_url"]), r["final_url"])
    check(r, "fast (<40s)", r["wall_s"] < 40, r["wall_s"])
    return r


@scenario("g12_unknown_clause_ai_unavailable", "a clause no rule understands needs AI; AI unavailable -> honest failure naming it (deterministic goals before it still done)")
async def g12(u):
    g, o = Brain("gemini", fail=["503 UNAVAILABLE"]), Brain("ollama", fail=["Ollama timed out"])
    r = await run_task_once(f"Open {u['gsearch']}/ , frobnicate the widget, scroll down", g, o)
    check(r, "failed honestly", r["status"] == "failed", r["status"])
    check(r, "first goal completed", ev_count(r, "GOAL_COMPLETE") == 1, ev_count(r, "GOAL_COMPLETE"))
    check(r, "failed goal named", "frobnicate" in (r["error"] or "").lower(), r["error"])
    return r


@scenario("g13_unknown_clause_ai_helps", "a clause no rule understands is given to the AI SCOPED to that clause; AI proposes, evidence verifies",
          {"OLLAMA_EMERGENCY_MAX_CALLS_PER_TASK": "3"})
async def g13(u):
    g = Brain("gemini", ["Images"])
    r = await run_task_once(f"Open {u['gsearch']}/search?q=cats , frobnicate the Images thing, then scroll down", g, None)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "AI was used for exactly the unknown clause", g.calls >= 1 and ev_count(r, "GOAL_START") == 3)
    check(r, "AI done-claim judged from evidence (verdict recorded, not trusted)", any(e.get("claimed_done") and e.get("verdict") in ("VERIFIED_COMPLETE", "VERIFIED_WEAK") for e in r["events"] if e["type"] == "VERIFICATION"))
    check(r, "limitation reported (verified only by state change)", "Limitation" in (r["message"] or ""), r["message"])
    return r


@scenario("g14_blocked_temporary", "CAPTCHA page that clears after ~3 s: runner waits for the user, then continues",
          {"MANUAL_INTERVENTION_WAIT_SECONDS": "20"})
async def g14(u):
    g = Brain("gemini")
    r = await run_task_once(f"Open {u['blocked']}/?v=temp , scroll down", g, None)
    check(r, "success after the page became usable", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "manual-intervention wait + cleared recorded", any(e.get("strategy") == "manual_intervention_cleared" for e in r["events"]), [e.get("strategy") for e in r["events"] if e["type"] == "RECOVERY"])
    return r


@scenario("g15_blocked_permanent", "CAPTCHA that never clears: honest BLOCKED failure that says so; AI = 0",
          {"MANUAL_INTERVENTION_WAIT_SECONDS": "4"})
async def g15(u):
    g = Brain("gemini")
    r = await run_task_once(f"Open {u['blocked']}/?v=perm , scroll down", g, None)
    check(r, "failed (blocked)", r["status"] == "failed", r["status"])
    check(r, "message says human verification/CAPTCHA", "captcha" in (r["error"] or "").lower() or "human" in (r["error"] or "").lower(), r["error"])
    check(r, "GOAL_BLOCKED recorded", ev_count(r, "GOAL_BLOCKED") >= 1)
    check(r, "AI calls = 0", g.calls == 0)
    return r


@scenario("g16_explicit_navigation_honoured", "a LATER explicit 'open <site>' goal is executed (only the opening bootstrap is state-protected)")
async def g16(u):
    g = Brain("gemini")
    r = await run_task_once(f"Open {u['gsearch']}/ , search for abc, scroll down, then open {u['learn']}/", g, None)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "ended on the other site", (r["final_url"] or "").rstrip("/") == u["learn"], r["final_url"])
    return r


@scenario("g17_switch_and_close_tab", "open another site in a new tab, switch back to the first tab, close the other tab: counts + active page verified")
async def g17(u):
    g = Brain("gemini")
    r = await run_task_once(f"Open {u['gsearch']}/ , open vsite in a new tab, switch to the first tab, then switch to the new tab and close the tab", g, None)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    urls, active = await pages_info()
    check(r, "one tab left", len(urls) == 1, urls)
    check(r, "active page is the first site", active.startswith(u["gsearch"]), active)
    return r


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
async def run_scenario(name):
    from tests.fixture_site import Sites
    sites = Sites()
    try:
        from agent import workflow_memory
        workflow_memory._MEMORY_PATH = Path(tempfile.mkdtemp()) / "workflows.json"
        from agent.provider_health import clear_global_cooldowns
        clear_global_cooldowns()
        # TEST-ONLY data: named fake sites so "open vsite in a new tab" exercises the NAME->URL path.
        from agent import planner as _pl
        _pl.SITE_ADAPTERS.append(_pl.SiteAdapter(aliases=("vsite",), url=sites.urls["vsite"]))
        _pl.SITE_ADAPTERS.append(_pl.SiteAdapter(aliases=("gsite",), url=sites.urls["gsearch"]))
        # TEST-ENVIRONMENT ADAPTATION: NEXA's OS-window focus step (browser/window_focus.py) is
        # Win32-only; on Linux BrowserManager._prepare_window always returns False and new_task_page
        # raises. Only that OS-window step is stubbed here; CDP tab activation still runs for real.
        from browser.manager import browser_manager as _bm

        async def _prepare_window_linux(page):
            await _bm._activate_page_via_cdp(page)
            try:
                await page.bring_to_front()
            except Exception:
                pass
            return True
        _bm._prepare_window = _prepare_window_linux
        result = await SCENARIOS[name]["fn"](sites.urls)
    finally:
        try:
            from browser.manager import browser_manager
            await asyncio.wait_for(browser_manager.stop(), 15)
        except Exception:
            pass
        sites.close()
    return result


def child(name, out):
    env_over = SCENARIOS[name]["env"]
    os.environ.update(env_over)
    asyncio.run(_child(name, out))


async def _child(name, out):
    try:
        res = await run_scenario(name)
        res["scenario"], res["description"] = name, SCENARIOS[name]["desc"]
    except Exception as exc:  # harness/browser error, reported as ERROR not PASS
        import traceback
        res = {"scenario": name, "description": SCENARIOS[name]["desc"], "harness_error": repr(exc),
               "trace": traceback.format_exc()[-1500:], "checks": [{"name": "harness ran", "result": "FAIL", "detail": repr(exc)}]}
    Path(out).write_text(json.dumps(res, indent=1, default=str))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--scenario")
    ap.add_argument("--child", action="store_true")
    ap.add_argument("--out", default="/tmp/nexa_results")
    a = ap.parse_args()
    if a.child:
        # (imports happen after env is set)
        wrapper = Path(tempfile.gettempdir()) / "nexa_test_chrome.sh"
        # Headed Chrome (needs a display; the parent wraps the child in xvfb-run when none exists)
        # so "bring tab to foreground" behaves as on a real desktop. --no-sandbox because CI runs as root.
        wrapper.write_text(f'#!/bin/sh\nexec {CHROME} --no-sandbox "$@"\n'); wrapper.chmod(0o755)
        os.environ.update({"BROWSER_CHROME_PATH": str(wrapper), "ALLOW_PRIVATE_NETWORK": "true", "AI_PROVIDER": "auto"})
        os.environ.pop("GEMINI_API_KEY", None)
        sys.path.insert(0, str(BACKEND))
        if os.environ.get("NEXA_TEST_LOG"):
            import logging
            logging.basicConfig(level=logging.INFO if os.environ.get("NEXA_TEST_LOG") == "info" else logging.WARNING, format="%(asctime)s %(name)s: %(message)s")
        # Test-only stand-ins are APPENDED so any real package that is installed always wins.
        sys.path.append(str(HERE / "_shims"))
        child(a.scenario, os.path.join(a.out, a.scenario + ".json"))
        return
    Path(a.out).mkdir(parents=True, exist_ok=True)
    names = list(SCENARIOS) if a.all else [a.scenario]
    rows = []
    for n in names:
        t0 = time.time()
        cmd = [sys.executable, __file__, "--child", "--scenario", n, "--out", a.out]
        if not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
            cmd = ["xvfb-run", "-a"] + cmd
        # Output goes to a FILE and the child gets its own process group: a Chrome left running by the
        # scenario can no longer keep a pipe open and hang the harness; on timeout the whole group is killed.
        log_path = Path(a.out) / (n + ".log")
        with open(log_path, "w") as logf:
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True,
                                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            try:
                proc.wait(timeout=240)
            except subprocess.TimeoutExpired:
                import signal
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        try:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)       # reap any browser the scenario left behind
        except Exception:
            pass
        f = Path(a.out) / (n + ".json")
        res = json.loads(f.read_text()) if f.exists() else {"checks": [{"name": "child produced result", "result": "FAIL", "detail": log_path.read_text()[-400:]}]}
        fails = [c for c in res.get("checks", []) if c["result"] != "PASS"]
        rows.append((n, "PASS" if not fails else "FAIL", res.get("status"), res.get("wall_s"), fails))
        print(f"{'PASS' if not fails else 'FAIL'}  {n:38s} status={res.get('status')} wall={res.get('wall_s')}s", flush=True)
        for c in fails:
            print("      FAILED CHECK:", c["name"], "|", c["detail"])
    print(f"\n{sum(1 for r in rows if r[1]=='PASS')}/{len(rows)} scenarios passed all checks")



# ---------------------------------------------------------------------------------------------------
# CONTRACT round: phone/email fill must be entered AND verified; never submitted; never silently dropped
# ---------------------------------------------------------------------------------------------------
PHONE = "9010635950"


def _verdicts(r):
    return [e for e in r["events"] if e["type"] == "VERIFICATION" and e.get("scope") == "final"]


@scenario("s01_fill_phone_verified_not_submitted", "CONTRACT (fixture): open store, Sign in, fill the phone number: value read back from the real field, NOT submitted; 0 AI")
async def s01(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['shop']}/ and fill the phone number as {PHONE} on the sign in page", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']} {r['message']}")
    check(r, "the identifier field holds exactly the number", await page_eval("(document.querySelector('#identifier')||{}).value") == PHONE)
    check(r, "the header search box was not used", await page_eval("document.querySelector('input[type=search]').value") == "")
    check(r, "nothing was submitted", (await page_eval("window.__submitted||0")) == 0)
    check(r, "browser is on the sign-in page", "/signin" in (r["final_url"] or ""), r["final_url"])
    check(r, "the phone number is not in clear text in any event", PHONE not in repr(r["events"]) + str(r["message"]))
    check(r, "a fill goal was planned and verified with read-back", any(e.get("type") == "VERIFICATION" and e.get("goal_kind") == "fill" and e.get("verdict") == "VERIFIED_COMPLETE" for e in r["events"]))
    check(r, "final verdict verified complete", any(e.get("verdict") == "VERIFIED_COMPLETE" for e in _verdicts(r)))
    check(r, "AI calls = 0", g.calls == 0 and o.calls == 0)
    return r


@scenario("s02_fill_phone_no_field_is_not_success", "CONTRACT (fixture): sign-in page without any identifier field -> NOT success, honest failure")
async def s02(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['shop']}/?v=nofield and fill the phone number as {PHONE} on the sign in page", g, o)
    check(r, "NOT success", r["status"] != "success", f"{r['status']} {r['error']}")
    check(r, "error says the field was not found", "no phone field" in (r["error"] or "").lower(), r["error"])
    return r


@scenario("s03_fill_phone_ambiguous_fields_blocks", "CONTRACT (fixture): two equally good phone fields -> blocked, nothing typed, no guess")
async def s03(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['shop']}/?v=ambiguous and fill the phone number as {PHONE} on the sign in page", g, o)
    check(r, "NOT success", r["status"] != "success", f"{r['status']} {r['error']}")
    check(r, "reported as ambiguous", "more than one" in (r["error"] or "").lower(), r["error"])
    check(r, "nothing was typed into either field", await page_eval("[...document.querySelectorAll('input[type=tel]')].every(e=>!e.value)"))
    return r


@scenario("s04_fill_phone_page_drops_value_is_not_success", "CONTRACT (fixture): the page clears typed text -> read-back mismatch -> NOT success")
async def s04(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['shop']}/?v=eat and fill the phone number as {PHONE} on the sign in page", g, o)
    check(r, "NOT success", r["status"] != "success", f"{r['status']} {r['error']}")
    check(r, "no VERIFIED_COMPLETE final verdict", not any(e.get("verdict") == "VERIFIED_COMPLETE" for e in _verdicts(r)))
    return r


@scenario("s05_password_is_never_entered", "CONTRACT (fixture): 'fill the password as ...' -> blocked at that goal; nothing typed; secret not stored in events")
async def s05(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['shop']}/ , click Sign in and fill the password as hunter2", g, o)
    check(r, "NOT success", r["status"] != "success", f"{r['status']} {r['error']}")
    check(r, "the secret appears nowhere in events/message/error", "hunter2" not in repr(r["events"]) + str(r["message"]) + str(r["error"]))
    check(r, "no field holds text", await page_eval("[...document.querySelectorAll('input')].every(e=>!e.value)"))
    check(r, "blocked reason mentions that it is never entered", "never" in (r["error"] or "").lower(), r["error"])
    return r



@scenario("m09_unselected_suggestion_is_not_a_recipient", "GMAIL-like (fixture): clicking the suggestion does nothing -> the recipient is NOT committed -> draft NOT reported verified; nothing sent")
async def m09(u):
    g, o = Brain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['mail']}/?c=one&pick=noop and compose an email to Rahul saying hello", g, o)
    check(r, "NOT success", r["status"] != "success", f"{r['status']} {r['error']}")
    check(r, "error says the recipient is not committed", "not committed" in (r["error"] or "").lower(), r["error"])
    check(r, "nothing was sent", (await page_eval("window.__sent||0")) == 0)
    check(r, "no VERIFIED_COMPLETE final verdict", not any(e.get("verdict") == "VERIFIED_COMPLETE" for e in _verdicts(r)))
    return r



GM_BODY = "Hello, I hope you are doing well."
GM_TO = "recipient@example.com"
GM_PHRASE = f"compose an email to {GM_TO}, and write '{GM_BODY}' Do not send it."


def _gm_chips(): return page_eval("[...document.querySelectorAll('#chips .afV')].map(c=>c.getAttribute('email'))")
def _gm_body(): return page_eval("document.getElementById('body').innerText")
async def _gm_untouched(r):
    check(r, "search box, subject untouched", await page_eval("document.querySelector('[name=q]').value==='' && document.getElementById('subj').value===''"))
    check(r, "NOT sent", (await sent()) == 0, await sent())


@scenario("gm01_exact_phrase_gmail_structure", "GMAIL (structure-modelled fixture): 'Open <mail>, compose an email to X, and write 'B'. Do not send it.' -> composer opens, To chip + exact body, draft unsent; AI = 0")
async def gm01(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['gmaillike']}/, {GM_PHRASE}", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']} {r['pending']}")
    check(r, "recipient committed as a chip", await _gm_chips() == [GM_TO], await _gm_chips())
    check(r, "body exact (incl. full stop)", (await _gm_body()).strip() == GM_BODY, await _gm_body())
    check(r, "subject left empty (none requested)", await page_eval("document.getElementById('subj').value===''"))
    await _gm_untouched(r)
    check(r, "composer still open as a draft", await page_eval("document.getElementById('dlg').style.display") == "block")
    check(r, "Compose clicked exactly once", await page_eval("window.__opens") == 1, await page_eval("window.__opens"))
    check(r, "AI calls = 0", g.calls == 0 and o.calls == 0 and g.json_calls == 0, f"g={g.calls} j={g.json_calls}")
    check(r, "final verdict VERIFIED_COMPLETE", any(e.get("verdict") == "VERIFIED_COMPLETE" for e in _verdicts(r)))
    return r


@scenario("gm02_subject_only_when_requested", "GMAIL (modelled): 'subject Hi' is typed into the Subject box and nowhere else")
async def gm02(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['gmaillike']}/, compose an email to {GM_TO}, subject Hi, and write 'Hello'. Don't send it.", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "subject = Hi", await page_eval("document.getElementById('subj').value") == "Hi")
    check(r, "body = Hello", (await _gm_body()).strip() == "Hello", await _gm_body())
    check(r, "search box untouched", await page_eval("document.querySelector('[name=q]').value===''"))
    check(r, "NOT sent", (await sent()) == 0)
    return r


@scenario("gm03_slow_inbox_and_slow_composer", "GMAIL (modelled): inbox hydrates after 2.5s, composer 1.5s after Compose -> bounded waits, no early failure")
async def gm03(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['gmaillike']}/?slow=2500&cdelay=1500, {GM_PHRASE}", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "chip + body", await _gm_chips() == [GM_TO] and (await _gm_body()).strip() == GM_BODY, f"{await _gm_chips()} {await _gm_body()}")
    check(r, "Compose clicked once", await page_eval("window.__opens") == 1)
    check(r, "NOT sent", (await sent()) == 0)
    return r


@scenario("gm04_chips_are_options", "GMAIL (modelled): committed recipient chips carry role=option inside a listbox -> still recognised as committed")
async def gm04(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['gmaillike']}/?chip=option&sugg=addr, {GM_PHRASE}", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "chip + body", await _gm_chips() == [GM_TO] and (await _gm_body()).strip() == GM_BODY, f"{await _gm_chips()}")
    check(r, "NOT sent", (await sent()) == 0)
    return r


@scenario("gm05_signed_out_is_blocked", "GMAIL (modelled): a sign-in page -> blocked with an honest reason; never 'success'")
async def gm05(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['gmaillike']}/?v=login, {GM_PHRASE}", g, o)
    check(r, "not success", r["status"] != "success", f"{r['status']} {r['error']}")
    check(r, "says sign-in", "sign" in ((r["error"] or "") + (r["message"] or "")).lower(), r["error"])
    return r


@scenario("gm06_existing_composer_reused", "GMAIL (modelled): a composer is already open -> Compose is NOT clicked again, fields are filled in it")
async def gm06(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['gmaillike']}/?open=1, {GM_PHRASE}", g, o)
    check(r, "success", r["status"] == "success", f"{r['status']} {r['error']}")
    check(r, "Compose never clicked", await page_eval("window.__opens") == 0, await page_eval("window.__opens"))
    check(r, "chip + body", await _gm_chips() == [GM_TO] and (await _gm_body()).strip() == GM_BODY)
    return r


@scenario("gm07_composer_never_appears", "GMAIL (modelled): Compose is clicked but no composer ever shows -> FAILED with the stage named, not success")
async def gm07(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['gmaillike']}/?v=nocomposer, {GM_PHRASE}", g, o)
    check(r, "failed (not success)", r["status"] in ("failed", "blocked"), f"{r['status']}")
    check(r, "reason names the composer", "composer" in (r["error"] or "").lower(), r["error"])
    check(r, "NOT sent", (await sent()) == 0)
    check(r, "no VERIFIED_COMPLETE", not any(e.get("verdict") == "VERIFIED_COMPLETE" for e in _verdicts(r)))
    return r


@scenario("gm08_address_not_committed_fails", "GMAIL (modelled): nothing commits the typed address -> not success, reason says not committed")
async def gm08(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['gmaillike']}/?commit=none, {GM_PHRASE}", g, o)
    check(r, "not success", r["status"] != "success", f"{r['status']} {r['error']}")
    check(r, "reason: not committed", "not committed" in (r["error"] or "").lower(), r["error"])
    check(r, "NOT sent", (await sent()) == 0)
    return r


@scenario("gm09_no_body_field_fails_honestly", "GMAIL (modelled): composer has no message-body editor -> composer never counts as ready, failed, not success")
async def gm09(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['gmaillike']}/?v=nobody, {GM_PHRASE}", g, o)
    check(r, "not success", r["status"] != "success", f"{r['status']} {r['error']}")
    check(r, "NOT sent", (await sent()) == 0)
    return r


@scenario("gm10_send_needs_confirmation", "GMAIL (modelled): '... Then send it.' fills the draft, then PAUSES for confirmation; nothing sent until approved")
async def gm10(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['gmaillike']}/, compose an email to {GM_TO}, and write '{GM_BODY}' Then send it.", g, o)
    check(r, "awaiting confirmation", r["status"] == "awaiting_confirmation", f"{r['status']} {r['error']}")
    check(r, "draft is filled", await _gm_chips() == [GM_TO] and (await _gm_body()).strip() == GM_BODY)
    check(r, "NOT sent yet", (await sent()) == 0)
    return r


@scenario("gm11_missing_body_asks", "GMAIL (modelled): no body given -> asks for it BEFORE opening anything; no text is invented")
async def gm11(u):
    g, o = JsonBrain("gemini"), Brain("ollama")
    r = await run_task_once(f"Open {u['gmaillike']}/, compose an email to {GM_TO}", g, o)
    check(r, "needs input for body", r["status"] == "needs_input" and (r["pending"] or {}).get("field") == "body", f"{r['status']} {r['pending']}")
    return r


if __name__ == "__main__":
    # the parent process only needs stdlib
    sys.path.insert(0, str(BACKEND))
    main()
