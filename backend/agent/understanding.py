"""Structured task understanding: natural language -> a VALIDATED representation the runner can execute.

    deterministic parse (agent.goals, free)  ->  TaskUnderstanding
        -> optional ONE bounded AI call (only when the parse is unsure) -> strict schema validation -> merged

What the representation holds: objective, ordered subgoals (each with expected result, preconditions, whether it
needs confirmation), website/destination, entities (recipient, subject, body, query, title, date, time...),
missing information, ambiguity, and the overall risk.  The AI never executes anything; its output is data that is
validated field by field and silently discarded if it does not fit the schema.

Stdlib only (plus agent.goals / agent.safety).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agent.contract import coverage_gaps, explicit_values, gap_question
from agent.goals import _EMAIL_ADDR, Goal, GoalKind, GoalStatus, decompose, redact_params
from agent.safety import says_do_not_send

RISK_NONE, RISK_DRAFT, RISK_CONSEQUENTIAL = "none", "draft", "consequential"
_ALLOWED_AI_KINDS = {k.value for k in GoalKind} - {GoalKind.SEND.value}   # the AI can never inject a SEND
_MAX_SUBGOALS = 12
_MAX_TEXT = 2000


class UnderstandingError(ValueError):
    pass


@dataclass
class Clarification:
    field: str
    question: str
    options: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"type": "clarification", "field": self.field, "question": self.question, "options": self.options}


@dataclass
class Subgoal:
    kind: str
    text: str
    params: dict
    expected: str = ""
    preconditions: list[str] = field(default_factory=list)
    requires_confirmation: bool = False

    def to_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "params": self.params, "expected": self.expected,
                "preconditions": self.preconditions, "requires_confirmation": self.requires_confirmation}


@dataclass
class TaskUnderstanding:
    instruction: str
    objective: str
    intent: str
    subgoals: list[Subgoal]
    site: Optional[str] = None
    entities: dict = field(default_factory=dict)
    missing: list[Clarification] = field(default_factory=list)
    risk: str = RISK_NONE
    requires_confirmation: bool = False
    confidence: float = 1.0
    source: str = "deterministic"
    goals: list[Goal] = field(default_factory=list)       # executable form of ``subgoals``

    def to_dict(self) -> dict:
        return {"objective": self.objective, "intent": self.intent, "site": self.site, "entities": self.entities,
                "subgoals": [s.to_dict() for s in self.subgoals], "missing": [m.to_dict() for m in self.missing],
                "risk": self.risk, "requires_confirmation": self.requires_confirmation,
                "confidence": round(self.confidence, 2), "source": self.source}


# --------------------------------------------------------------------------------------------------
# expected results & preconditions (what evidence will satisfy the subgoal)
# --------------------------------------------------------------------------------------------------
def expected_result(g: Goal) -> str:
    k, p = g.kind, g.params
    return {
        GoalKind.NAVIGATE: "the requested site is the active page",
        GoalKind.SEARCH: f'the query "{p.get("query")}" was submitted and results/target page are shown',
        GoalKind.TYPE: f'the targeted field contains exactly "{p.get("text")}"',
        GoalKind.CLICK: "the page changed because of the click (new page/URL/content)",
        GoalKind.COMPOSE: "a composer shows the recipient, subject and body as typed; nothing is sent",
        GoalKind.SEND: "the message is sent - only after explicit user confirmation",
        GoalKind.EXTRACT: "the requested text is read from the live page and re-checked in the DOM",
        GoalKind.BACK: "the previous page of this tab is shown",
        GoalKind.FORWARD: "the next page of this tab is shown",
    }.get(k, "the page reflects the requested step")


def _preconditions(g: Goal, prev: list[Goal]) -> list[str]:
    pre: list[str] = []
    if g.kind in (GoalKind.COMPOSE, GoalKind.SEND, GoalKind.TYPE, GoalKind.CLICK, GoalKind.SEARCH) and any(
            x.kind in (GoalKind.NAVIGATE, GoalKind.SEARCH) for x in prev):
        pre.append("the previous step has reached its expected page")
    if g.kind == GoalKind.SEND:
        pre.append("a composed draft is open")
    if g.kind == GoalKind.COMPOSE:
        pre.append("the user is signed in to the mail/message service (otherwise the task reports a blocker)")
    return pre


# --------------------------------------------------------------------------------------------------
# recipients
# --------------------------------------------------------------------------------------------------
_RELATIONSHIP = re.compile(r"^(?:my|our|the|his|her|their)\s+\w+(?:\s+\w+)?$", re.I)


def classify_recipient(text: str | None) -> str:
    """address | name | relationship | none.  A relationship ("my professor") cannot be resolved from the page."""
    t = (text or "").strip()
    if not t:
        return "none"
    if _EMAIL_ADDR.fullmatch(t):
        return "address"
    if _RELATIONSHIP.match(t):
        return "relationship"
    if re.fullmatch(r"[A-Za-z][A-Za-z.'\- ]{0,60}", t):
        return "name"
    return "name"


# --------------------------------------------------------------------------------------------------
# deterministic understanding
# --------------------------------------------------------------------------------------------------
_DATE = re.compile(r"\b(today|tomorrow|tonight|next\s+\w+|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\b", re.I)
_TIME = re.compile(r"\b(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)|noon|midnight)\b", re.I)
_EVENT = re.compile(r"\b(?:create|add|schedule|make|set\s+up|book)\s+(?:a\s+|an\s+|new\s+)*(?:calendar\s+)?(?:event|meeting|appointment|reminder)\b", re.I)
_EVENT_TITLE = re.compile(r"\b(?:called|named|titled|for|about)\s+(?P<t>[\"'“]?[^\"'”,.]+?)[\"'”]?(?=\s+(?:tomorrow|today|on|at|next)\b|$)", re.I)
_FIRST_SENT_SITE = re.compile(r"\b(?:open|go\s+to|visit|launch)\s+(?:my\s+)?(?P<s>[\w.\- ]+?)(?=\s+(?:and|then|,)|[,.]|$)", re.I)


def _entities(instruction: str, goals: list[Goal]) -> dict:
    ent: dict[str, Any] = {}
    for g in goals:
        p = g.params
        if g.kind == GoalKind.SEARCH and p.get("query"):
            ent.setdefault("query", p["query"])
        if g.kind == GoalKind.TYPE and p.get("text"):
            ent.setdefault("text", p["text"])
        if g.kind == GoalKind.COMPOSE:
            for k in ("recipient", "subject", "body", "purpose", "channel"):
                if p.get(k):
                    ent[k] = p[k]
            if p.get("recipient"):
                ent["recipient_kind"] = classify_recipient(p["recipient"])
    m = _DATE.search(instruction)
    if m and re.search(r"\b(?:event|meeting|appointment|calendar|reminder|schedule)\b", instruction, re.I):
        ent["date"] = m.group(1)
    t = _TIME.search(instruction)
    if t and "date" in ent:
        ent["time"] = t.group(1)
    return ent


_LOGIN = re.compile(r"\b(?:log\s?in|sign\s?in|login|signin)\b", re.I)
_FILL_FORM = re.compile(r"\b(?:fill|enter|type|put|complete|help)\b.*\b(?:form|details|credentials|password|username|login|sign\s?in|log\s?in)\b|\b(?:log|sign)\s?in\b", re.I)


def _is_login_request(instruction: str, goals: list[Goal]) -> bool:
    text = re.sub(r"https?://\S+", " ", instruction)         # a URL such as ?v=login is not the user's words
    return bool(_LOGIN.search(text) and _FILL_FORM.search(text)
                and not any(g.kind in (GoalKind.COMPOSE, GoalKind.SEND) for g in goals))


LOGIN_NOTE = ("I stopped before submitting anything. NEXA never types passwords or one-time codes from a request, and it "
              "does not get past CAPTCHAs or MFA, so please finish signing in yourself in the open tab.")


def _login_goals(instruction: str, goals: list[Goal], resolver) -> list[Goal]:
    """Sign-in assistance KEEPS every goal the user stated.

    Only the clause that merely says "open the login page" is replaced by (navigate + a DOM-resolved click on the
    sign-in control).  Fill goals for ordinary identifiers stay; password / OTP goals stay too and are reported as
    BLOCKED when reached - they are never dropped and never automated.
    """
    def opens_login_only(g: Goal) -> bool:
        if g.kind == GoalKind.ADAPTIVE or (g.kind == GoalKind.CLICK and g.params.get("maybe_site")):
            return bool(_LOGIN.search(g.text)) and not explicit_values(g.text)
        return False

    nav = [g for g in goals if g.kind in (GoalKind.NAVIGATE, GoalKind.NEW_TAB)][:1]
    if not nav and resolver is not None:      # "I need to log in to Amazon": the destination is a KNOWN site named in the text
        for w in (x.strip(".-") for x in re.findall(r"[A-Za-z][A-Za-z0-9.-]*", instruction)):
            url = resolver(w.lower())
            if url:
                nav = [Goal(id=1, kind=GoalKind.NAVIGATE, text=f"open {w}", params={"url": url, "site": w.lower()})]
                break
    click = Goal(id=0, kind=GoalKind.CLICK, text="open the sign-in page", params={"target": "Sign in", "login_page": True})
    rest = [g for g in goals if g not in nav and not opens_login_only(g)]
    already_signin = any(g.kind == GoalKind.CLICK and (g.params.get("login_page") or _LOGIN.search(str(g.params.get("target") or "")))
                         for g in rest)
    out = nav + ([] if already_signin else [click]) + rest
    for i, g in enumerate(out, 1):
        g.id = i
    return out


def _intent(instruction: str, goals: list[Goal]) -> str:
    kinds = [g.kind for g in goals]
    if _is_login_request(instruction, goals):
        return "login_assist"
    if GoalKind.SEND in kinds and GoalKind.COMPOSE not in kinds:
        return "send_message"
    if GoalKind.COMPOSE in kinds:
        return "compose_and_send" if GoalKind.SEND in kinds else "compose_draft"
    if _EVENT.search(instruction):
        return "create_event"
    if GoalKind.EXTRACT in kinds:
        return "extract_information"
    if GoalKind.SEARCH in kinds:
        return "search_and_navigate"
    if kinds and all(k in (GoalKind.NAVIGATE, GoalKind.NEW_TAB) for k in kinds):
        return "navigate"
    return "browse"


def understand(instruction: str, resolver: Optional[Callable[[str], Optional[str]]] = None) -> TaskUnderstanding:
    """Deterministic understanding.  ``confidence`` < 0.7 means a clause was not understood (ADAPTIVE) or an essential
    detail needs the user; the orchestrator may then spend ONE bounded AI call to improve it."""
    instruction = " ".join(str(instruction or "").split())
    goals = decompose(instruction, resolver)
    intent = _intent(instruction, goals)
    if intent == "login_assist":
        goals = _login_goals(instruction, goals, resolver)
    ent = _entities(instruction, goals)
    missing: list[Clarification] = []
    sites = [g.params.get("site") or g.params.get("url") for g in goals if g.kind in (GoalKind.NAVIGATE, GoalKind.NEW_TAB)]
    site = next((s for s in sites if s), None)

    # ---- message composition: essential details ------------------------------------------------
    for g in goals:
        if g.kind != GoalKind.COMPOSE:
            continue
        p = g.params
        rk = classify_recipient(p.get("recipient"))
        if rk == "none":
            missing.append(Clarification("recipient", "Who should the " + p.get("channel", "message") + " go to? (a name or an email address)"))
        elif rk == "relationship":
            missing.append(Clarification("recipient", f'I can\'t tell who "{p["recipient"]}" is from the page. Which email address or contact name should I use?'))
        if not p.get("body"):
            if p.get("purpose"):
                p["needs_authoring"] = True          # body must be written: AI (if available) else ask
            else:
                missing.append(Clarification("body", "What should the " + p.get("channel", "message") + " say?"))
        if says_do_not_send(g.text):
            p["no_send"] = True
    # ---- field fills ----------------------------------------------------------------------------
    for g in goals:
        if g.kind == GoalKind.FILL and not g.params.get("secret") and not g.params.get("value"):
            fld = g.params.get("field")
            missing.append(Clarification(str(fld), f"Which {'phone number' if fld == 'phone' else 'email address'} should I enter?"))
    # ---- completeness: no explicit value of the user may be silently dropped ---------------------
    gaps = coverage_gaps(instruction, goals)
    if gaps:
        missing.append(Clarification("requirement", gap_question(gaps)))
        ent["uncovered"] = [{"kind": x["kind"]} for x in gaps]
    # ---- calendar event ------------------------------------------------------------------------
    if intent == "create_event":
        tm = _EVENT_TITLE.search(instruction)
        if tm:
            ent["title"] = tm.group("t").strip()
        else:
            missing.append(Clarification("title", "What should the event be called?"))
        if "date" not in ent:
            missing.append(Clarification("date", "Which day is the event on?"))

    risk = RISK_DRAFT if intent == "login_assist" else RISK_NONE
    if intent == "login_assist":
        ent["note"] = LOGIN_NOTE
    if any(g.kind == GoalKind.COMPOSE for g in goals):
        risk = RISK_DRAFT
    needs_conf = any(g.kind == GoalKind.SEND for g in goals)
    if needs_conf:
        risk = RISK_CONSEQUENTIAL
    # a SEND after a no-send compose is contradictory: never send; surface it
    if any(g.kind == GoalKind.COMPOSE and g.params.get("no_send") for g in goals) and needs_conf:
        goals = [g for g in goals if g.kind != GoalKind.SEND]
        needs_conf = False
        risk = RISK_DRAFT

    subgoals = []
    for i, g in enumerate(goals):
        subgoals.append(Subgoal(g.kind.value, g.text, redact_params({k: v for k, v in g.params.items() if not k.startswith("_")}),
                                expected_result(g), _preconditions(g, goals[:i]), g.kind == GoalKind.SEND))
    unknown = sum(1 for g in goals if g.kind == GoalKind.ADAPTIVE)
    confidence = 1.0 if not goals else max(0.2, 1.0 - 0.35 * unknown / max(1, len(goals)) - 0.25 * bool(missing))
    if not goals:
        confidence = 0.1
    objective = _objective(intent, ent, site, instruction)
    return TaskUnderstanding(instruction=instruction, objective=objective, intent=intent, subgoals=subgoals, site=site,
                             entities=ent, missing=missing, risk=risk, requires_confirmation=needs_conf,
                             confidence=confidence, goals=goals)


def _objective(intent: str, ent: dict, site: str | None, instruction: str) -> str:
    where = f" on {site}" if site else ""
    if intent.startswith("compose"):
        return f"Prepare a {ent.get('channel', 'message')} draft" + (f" to {ent['recipient']}" if ent.get("recipient") else "") + where + \
               (" and send it after confirmation" if intent == "compose_and_send" else " (not sent)")
    if intent == "send_message":
        return "Send the open message after the user confirms"
    if intent == "search_and_navigate" and ent.get("query"):
        return f'Search for "{ent["query"]}"{where} and reach the requested page'
    from agent.contract import redact_instruction
    return redact_instruction(instruction)[:140]


# --------------------------------------------------------------------------------------------------
# AI-assisted understanding: strict schema, bounded, never authoritative
# --------------------------------------------------------------------------------------------------
SCHEMA_HINT = {
    "objective": "string (<=200 chars)",
    "intent": "string",
    "site": "string|null (a website NAME or an https URL)",
    "entities": {"recipient": "string?", "subject": "string?", "body": "string?", "query": "string?", "title": "string?", "date": "string?", "time": "string?"},
    "subgoals": [{"kind": "one of: " + ", ".join(sorted(_ALLOWED_AI_KINDS)), "text": "the clause", "params": {"url|query|text|target|recipient|subject|body": "string"},
                  "expected": "string"}],
    "missing": [{"field": "string", "question": "concise question for the user"}],
}


def build_understanding_prompt(instruction: str, deterministic: TaskUnderstanding) -> str:
    return (
        "You convert a user's browser-automation request into a JSON plan of ordered subgoals. Reply with ONE JSON object only.\n"
        "Rules: never invent an email address, URL, recipient or message text the user did not give; if an essential detail "
        "is missing put it in `missing` with a short question. Compose/draft means DRAFT only - never emit a send step. "
        "Keep `search for A and B` as ONE search query. At most 12 subgoals.\n"
        f"Schema: {json.dumps(SCHEMA_HINT)}\n"
        f"Request: {json.dumps(__import__('agent.contract', fromlist=['x']).redact_instruction(instruction, mask_ids=False))}\n"
        f"Deterministic parse (may be incomplete): {json.dumps(deterministic.to_dict())[:1500]}\n"
    )


def _s(v: Any, limit: int = _MAX_TEXT) -> str:
    if not isinstance(v, str):
        raise UnderstandingError("expected a string")
    v = v.strip()
    if len(v) > limit:
        raise UnderstandingError("string too long")
    return v


def validate_ai_understanding(data: Any, instruction: str) -> TaskUnderstanding:
    """Strict validation of a model's JSON.  Raises UnderstandingError on ANY deviation (caller falls back)."""
    if not isinstance(data, dict):
        raise UnderstandingError("not an object")
    raw_sub = data.get("subgoals")
    if not isinstance(raw_sub, list) or not raw_sub or len(raw_sub) > _MAX_SUBGOALS:
        raise UnderstandingError("subgoals must be a list of 1..12")
    goals: list[Goal] = []
    subgoals: list[Subgoal] = []
    for i, sg in enumerate(raw_sub):
        if not isinstance(sg, dict):
            raise UnderstandingError("subgoal must be an object")
        kind = _s(sg.get("kind"), 30).lower()
        if kind not in _ALLOWED_AI_KINDS:
            raise UnderstandingError(f"disallowed subgoal kind {kind!r}")
        params_in = sg.get("params") or {}
        if not isinstance(params_in, dict):
            raise UnderstandingError("params must be an object")
        params: dict[str, Any] = {}
        for k, v in params_in.items():
            if k not in {"url", "query", "text", "target", "recipient", "subject", "body", "site", "key", "direction", "channel",
                         "ordinal", "what", "request", "purpose", "seconds", "field", "value"}:
                continue
            if isinstance(v, (int, float)) and k in {"ordinal", "seconds"}:
                params[k] = v
                continue
            params[k] = _s(v)
        if kind == GoalKind.FILL.value:
            # the AI may only propose an ORDINARY identifier whose value is literally in the user's words
            if params.get("field") not in ("phone", "email") or not params.get("value"):
                raise UnderstandingError("fill goals need field=phone|email and a value")
            vals = [v for k, v in explicit_values(instruction) if k == params["field"]]
            if not any(re.sub(r"\D", "", v) == re.sub(r"\D", "", params["value"]) if params["field"] == "phone"
                       else v.lower() == params["value"].lower() for v in vals):
                raise UnderstandingError("fill value does not appear in the user's request")
        url = params.get("url")
        if url and not re.match(r"^https?://", url, re.I):
            raise UnderstandingError("only http(s) URLs are allowed")
        if kind == GoalKind.COMPOSE.value:
            params.setdefault("send", False)
            params["send"] = False                     # compose never sends, whatever the model says
            if params.get("recipient") and classify_recipient(params["recipient"]) == "address" and not _EMAIL_ADDR.fullmatch(params["recipient"]):
                raise UnderstandingError("bad address")
        text = _s(sg.get("text") or kind, 400)
        g = Goal(id=i + 1, kind=GoalKind(kind), text=text, params=params)
        goals.append(g)
        subgoals.append(Subgoal(kind, text, params, _s(sg.get("expected") or expected_result(g), 300), _preconditions(g, goals[:-1])))
    miss = []
    for m in data.get("missing") or []:
        if isinstance(m, dict) and m.get("question"):
            miss.append(Clarification(_s(m.get("field") or "detail", 40), _s(m["question"], 300)))
    ent_in = data.get("entities") or {}
    ent = {k: _s(v, 500) for k, v in ent_in.items() if isinstance(ent_in, dict) and isinstance(v, str) and v.strip()}
    # a body or recipient the model "found" must literally come from the user's words unless it is a name for the draft
    risk = RISK_DRAFT if any(g.kind == GoalKind.COMPOSE for g in goals) else RISK_NONE
    return TaskUnderstanding(instruction=instruction, objective=_s(data.get("objective") or instruction[:140], 200),
                             intent=_s(data.get("intent") or "browse", 60), subgoals=subgoals,
                             site=(_s(data["site"], 200) if isinstance(data.get("site"), str) and data["site"].strip() else None),
                             entities=ent, missing=miss, risk=risk, requires_confirmation=False, confidence=0.8,
                             source="ai", goals=goals)


def merge(det: TaskUnderstanding, ai: TaskUnderstanding) -> TaskUnderstanding:
    """Prefer the AI decomposition only where the deterministic one did not understand - and only if it is VALIDATED:
    the AI plan must not lose any requirement the deterministic parse already represents.  Neither side silently
    overwrites the other; a rejected AI plan leaves the deterministic one in force."""
    gaps_det = coverage_gaps(det.instruction, det.goals)
    gaps_ai = coverage_gaps(det.instruction, ai.goals)
    must_keep = {GoalKind.FILL, GoalKind.SEARCH, GoalKind.COMPOSE, GoalKind.TYPE}
    lost = [k.value for k in must_keep if any(g.kind == k for g in det.goals) and not any(g.kind == k for g in ai.goals)]
    secret_fills = [g for g in det.goals if g.kind == GoalKind.FILL and g.params.get("secret")]
    if len(gaps_ai) > len(gaps_det) or lost or (secret_fills and not any(g.params.get("secret") for g in ai.goals if g.kind == GoalKind.FILL)):
        det.entities["ai_rejected"] = ("the AI plan dropped requirements: " + ", ".join(lost or ["explicit values"]))
        return det
    if det.requires_confirmation and not ai.requires_confirmation:
        # the deterministic parse saw an explicit send: keep that gate
        ai.requires_confirmation, ai.risk = True, RISK_CONSEQUENTIAL
    if any(g.kind == GoalKind.COMPOSE and g.params.get("no_send") for g in det.goals):
        for g in ai.goals:
            if g.kind == GoalKind.COMPOSE:
                g.params["no_send"] = True
    if gaps_ai:
        ai.missing = ai.missing or [Clarification("requirement", gap_question(gaps_ai))]
    else:
        ai.missing = [m for m in ai.missing if m.field != "requirement"] or ([] if not det.missing else
                                                                          [m for m in det.missing if m.field not in ("requirement",)])
    return ai


# Authoring a message body from a stated purpose ("explaining that I will be absent tomorrow")
def build_body_prompt(purpose: str, recipient: str | None, channel: str) -> str:
    return ("Write the body of a short, polite " + channel + (f" to {recipient}" if recipient else "") +
            f" that conveys exactly this and nothing else: {json.dumps(purpose)}.\n"
            'Do not invent facts, names, dates or promises. No subject line. Reply with ONE JSON object: {"body": "..."}')


def validate_body(data: Any, purpose: str) -> str:
    if not isinstance(data, dict):
        raise UnderstandingError("not an object")
    body = _s(data.get("body"), 1200)
    if len(body) < 3:
        raise UnderstandingError("empty body")
    return body
