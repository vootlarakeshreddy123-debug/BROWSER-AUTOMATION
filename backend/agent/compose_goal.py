"""Generic message composition and send, executed on the LIVE DOM (no website names, no site selectors).

COMPOSE   find/open a composer -> recipient (resolve autocomplete from evidence) -> optional subject -> body ->
          independently read every value back.  The result is a DRAFT: nothing is sent.
SEND      never executes without the user's explicit confirmation (the runner pauses with a ConfirmationRequest).

Ambiguity is never guessed: two different matching contacts -> the task pauses and asks which one; no match for a name
-> asks for an address; a relationship noun ("my professor") -> asks.  Sign-in walls / CAPTCHA are reported as blockers.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from agent import events as ev
from agent.action_schema import Action, ActionType, Target
from agent.field_ranker import BODY, RECIPIENT, SUBJECT, Candidate, rank_fields, target_dict, verify_in_field
from agent.goals import _EMAIL_ADDR, Goal, GoalKind
from agent.goal_verify import Verdict
from agent.planner import _interactive_target
from agent.safety import SEND, ConfirmationRequest
from agent.understanding import Clarification, classify_recipient

OPENERS = r"^(?:compose|new\s+(?:message|e-?mail|mail|chat)|write(?:\s+(?:a\s+)?(?:new\s+)?(?:message|e-?mail|mail))?|create\s+(?:new\s+)?(?:message|e-?mail|mail)|new|start\s+(?:a\s+)?(?:new\s+)?(?:message|chat))$"
_SUGGESTION_ROLES = {"option", "menuitem", "listitem", "row", "gridcell", "treeitem"}
_STRUCT_REGIONS = {"header", "footer", "nav"}

DOM_CONTAINS_JS = r"""
(needles) => {
  const n = (v) => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const hay = [n(document.body ? document.body.innerText : '')];
  for (const e of document.querySelectorAll('input,textarea,[contenteditable]')) hay.push(n(e.value || e.innerText || e.textContent));
  const all = hay.join(' | ');
  return needles.map(x => all.includes(n(x)));
}
"""


# Text that is really COMMITTED in the composer: field values and non-suggestion content.  Suggestion lists (role=option /
# listbox) are excluded, so an un-selected autocomplete row can never be mistaken for a chosen recipient.
COMMITTED_JS = r"""
(needles) => {
  const n = (v) => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const inList = (el) => !!el.closest('[role=listbox],[role=option],datalist,ul[role=listbox]');
  const hay = [];
  for (const e of document.querySelectorAll('input,textarea')) if (!inList(e)) hay.push(n(e.value));
  for (const e of document.querySelectorAll('[contenteditable],.chip,[data-chip],[role=gridcell],[role=row],span,div')) {
    if (inList(e) || e.children.length > 6) continue;
    hay.push(n(e.innerText || e.textContent));
  }
  const all = hay.join(' | ');
  return needles.map(x => all.includes(n(x)));
}
"""


# State of the recipient control, read from the live DOM.  A typed address is NOT a recipient until the composer turned
# it into one: a chip / hidden "to" value / email attribute, or - for a plain input with no autocomplete popup - the value.
RECIPIENT_STATE_JS = r"""
({xpath, placeholder, label, tokens}) => {
  const n = (v) => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  let el = null;
  if (xpath) { try { el = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue; } catch (e) {} }
  if (!el && placeholder) el = Array.from(document.querySelectorAll('[placeholder]')).find(e => e.getAttribute('placeholder') === placeholder) || null;
  if (!el && label) el = Array.from(document.querySelectorAll('[aria-label]')).find(e => e.getAttribute('aria-label') === label) || null;
  const raw = el ? ((el.value !== undefined && el.value !== '') ? el.value : (el.innerText || el.textContent || '')) : '';
  const visible = (e) => { const r = e.getBoundingClientRect(), st = getComputedStyle(e); return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const inList = (e) => !!e.closest('[role=listbox],[role=option],datalist');
  const isCombo = !!el && (el.getAttribute('role') === 'combobox' || el.hasAttribute('aria-haspopup') || el.hasAttribute('aria-autocomplete') || el.hasAttribute('list'));
  const attrs = ['email', 'data-email', 'data-hovercard-id', 'data-name', 'data-recipient'];
  const per = tokens.map((raw0) => {
    const t = n(raw0); const r = {attr: false, hidden: false, outside: false, inl: false};
    for (const e of document.querySelectorAll('[email],[data-email],[data-hovercard-id],[data-name],[data-recipient]')) {
      if (e === el) continue;
      for (const a of attrs) if (n(e.getAttribute(a)).includes(t)) r.attr = true;
    }
    for (const e of document.querySelectorAll('input,textarea')) {
      if (e === el) continue;
      if (/^(?:to|recipients?|toaddr\w*)$/.test(n(e.getAttribute('name'))) && n(e.value).includes(t)) r.hidden = true;
    }
    for (const e of document.querySelectorAll('body *')) {
      if (['SCRIPT', 'STYLE', 'INPUT', 'TEXTAREA', 'OPTION'].includes(e.tagName) || e.children.length > 3) continue;
      if (el && (e === el || el.contains(e))) continue;
      if (e.closest('[contenteditable]') || !visible(e)) continue;
      if (!n(e.innerText || e.textContent).includes(t)) continue;
      if (inList(e)) r.inl = true; else r.outside = true;
    }
    return r;
  });
  return {found: !!el, value: n(raw), isCombo, expanded: !!el && el.getAttribute('aria-expanded') === 'true', per};
}
"""

_SIGNIN_URL = re.compile(r"^https?://accounts\.|/(?:v\d+/)?(?:signin|sign-in|login|log-in|servicelogin|identifier)(?:[/?#]|$)", re.I)
_SIGNIN_TITLE = re.compile(r"\b(?:sign[\s-]?in|log[\s-]?in)\b", re.I)

OPENER_WAIT_S = 8.0          # a webmail inbox hydrates after the page "loads": wait (bounded) for the Compose control
COMPOSER_WAIT_S = 8.0        # ... and the composer itself appears asynchronously after it is clicked


class GoalNeedsInput(Exception):
    def __init__(self, clarification: Clarification) -> None:
        super().__init__(clarification.question)
        self.clarification = clarification


class GoalNeedsConfirmation(Exception):
    def __init__(self, request: ConfirmationRequest) -> None:
        super().__init__(request.description)
        self.request = request


def _suggestion_text(e: dict) -> str:
    return " ".join(str(e.get("name") or e.get("text") or "").split())


class ComposeMixin:
    # ---------------------------------------------------------------- helpers
    async def _dom_contains(self, *needles: str) -> list[bool]:
        try:
            return list(await self.page.evaluate(DOM_CONTAINS_JS, [str(x) for x in needles]))
        except Exception:
            return [False] * len(needles)

    async def _committed_contains(self, *needles: str) -> list[bool]:
        try:
            return list(await self.page.evaluate(COMMITTED_JS, [str(x) for x in needles]))
        except Exception:
            return [False] * len(needles)

    def _composer_ready(self, ctx: dict) -> bool:
        r = rank_fields(ctx, RECIPIENT, "recipient", allow_weak_fallback=False).best()
        b = rank_fields(ctx, BODY, "message body", allow_weak_fallback=False).best()
        return bool(r and b)

    @staticmethod
    def _signed_out(ctx: dict) -> bool:
        """The page is a sign-in page (so no composer can exist): URL / title / password field / login controls."""
        from agent.blocked_page import looks_like_login_wall
        url = str(ctx.get("current_url") or ctx.get("url") or "")
        title = str(ctx.get("current_title") or ctx.get("title") or "")
        els = [e for e in (ctx.get("elements") or []) if isinstance(e, dict) and e.get("visible") is not False]
        password = any(str(e.get("input_type") or e.get("type") or "").lower() == "password" for e in els)
        return bool(_SIGNIN_URL.search(url) or _SIGNIN_TITLE.search(title) or password or looks_like_login_wall(ctx))

    async def _open_composer(self, goal: Goal) -> dict:
        """Return a context in which the composer is visible, opening it when needed.  Every wait is bounded and every
        failure names the stage (page not ready / sign-in / composer did not appear)."""
        from agent.goal_runner import GoalBlocked, GoalFailed, _find_by_text
        ctx, _, _ = await self._observe()
        await self._guard_blocked(goal, ctx)
        deadline = time.monotonic() + OPENER_WAIT_S
        while True:
            if self._composer_ready(ctx):                    # an existing composer is reused: Compose is NOT clicked again
                self.rec(ev.OBSERVE, goal, stage="composer_ready", reused_existing=True, url=self._url())
                return ctx
            opener = _find_by_text(ctx, OPENERS, roles=("button", "link", "menuitem"))
            if opener is not None:
                break
            if self._signed_out(ctx):
                raise GoalBlocked("A sign-in page is shown, so the message cannot be composed. Sign in in the visible browser, then run the task again.")
            if time.monotonic() >= deadline:
                await self._guard_blocked(goal, ctx)
                raise GoalFailed(f"no way to start a new message appeared within {int(OPENER_WAIT_S)}s (the page may still be loading or has no composer)")
            await asyncio.sleep(0.5)
            ctx, _, _ = await self._observe()
        r = await self._exec(goal, Action(type=ActionType.CLICK, target=_interactive_target(opener, fallback_role="button"),
                                          reason=f"Open the composer ('{str(opener.get('name'))[:40]}')"))
        if not r["ok"]:
            raise GoalFailed(f"could not open the composer: {r['error']}")
        deadline = time.monotonic() + COMPOSER_WAIT_S
        while True:
            ctx, _, _ = await self._observe()
            if self._composer_ready(ctx):
                self.rec(ev.OBSERVE, goal, stage="composer_ready", reused_existing=False, url=self._url())
                return ctx
            if time.monotonic() >= deadline:
                await self._guard_blocked(goal, ctx)
                if self._signed_out(ctx):
                    raise GoalBlocked("A sign-in page is shown, so the message cannot be composed. Sign in in the visible browser, then run the task again.")
                raise GoalFailed(f"the composer did not appear (no recipient field and message-body editor within {int(COMPOSER_WAIT_S)}s of opening it)")
            await asyncio.sleep(0.4)

    async def _type_field(self, goal: Goal, ctx: dict, intent: str, hint: str, text: str, what: str) -> Candidate:
        ranking = rank_fields(ctx, intent, hint, allow_weak_fallback=False)
        from agent.goal_runner import GoalFailed
        if not ranking.candidates:
            raise GoalFailed(f"no {what} field was found in the composer")
        for cand in ranking.candidates[:3]:
            tgt = target_dict(cand, ranking.candidates)
            if not tgt:
                continue
            self.rec(ev.OBSERVE, goal, field_candidate=cand.label[:60], intent=intent, score=round(cand.score, 1), reasons=cand.reasons[:3])
            r = await self._exec(goal, Action(type=ActionType.TYPE, text=text, target=Target(**tgt), reason=f"Type the {what}"))
            if not r["ok"]:
                self.rec(ev.RECOVERY, goal, level=3, strategy="next_field_candidate", reason=str(r["error"])[:140], url=self._url())
                continue
            chk = await verify_in_field(self.page, cand, text)
            if chk.get("ok"):
                return cand
            self.rec(ev.RECOVERY, goal, level=3, strategy="next_field_candidate",
                     reason=f"the {what} field did not hold the text", url=self._url())
        raise GoalFailed(f"could not enter the {what}: no candidate field held the text afterwards")

    def _suggestions(self, ctx: dict, name: str, field_el: dict | None) -> list[dict]:
        toks = [t for t in re.findall(r"[a-z0-9@._\-]+", name.lower()) if len(t) > 1]
        out = []
        for e in ctx.get("elements") or []:
            if not isinstance(e, dict) or e.get("visible") is False or e.get("disabled"):
                continue
            role = str(e.get("role") or "").lower()
            tag = str(e.get("tag") or "").lower()
            if role not in _SUGGESTION_ROLES and tag not in {"li", "option"}:
                continue
            if str(e.get("region") or "") in _STRUCT_REGIONS:
                continue
            text = _suggestion_text(e)
            if not text or not toks:
                continue
            low = text.lower()
            if all(t in low for t in toks):
                out.append(e)
        return out

    async def _resolve_recipient(self, goal: Goal, ctx: dict, recipient: str) -> str:
        from agent.goal_runner import GoalFailed
        kind = classify_recipient(recipient)
        cand = await self._type_field(goal, ctx, RECIPIENT, "recipient", recipient, "recipient")
        # let autocomplete settle, then judge ONLY from what the page offers
        matches: list[dict] = []
        for _ in range(5):
            await asyncio.sleep(0.4)
            c2, _, _ = await self._observe()
            matches = self._suggestions(c2, recipient, cand.element)
            if matches:
                ctx = c2
                break
        # distinct contacts (by e-mail address when shown, else by text)
        distinct: dict[str, dict] = {}
        for m in matches:
            t = _suggestion_text(m)
            key = (_EMAIL_ADDR.search(t).group(0).lower() if _EMAIL_ADDR.search(t) else t.lower())
            distinct.setdefault(key, m)
        if kind == "address":
            exact = [m for m in matches if recipient.lower() in _suggestion_text(m).lower()]
            if exact:
                await self._click_suggestion(goal, exact[0])
            else:
                await self._commit_with_key(goal, cand)
        elif len(distinct) == 1:
            await self._click_suggestion(goal, next(iter(distinct.values())))
        elif len(distinct) > 1:
            opts = [_suggestion_text(m)[:90] for m in distinct.values()][:6]
            raise GoalNeedsInput(Clarification("recipient", f'More than one contact matches "{recipient}". Which one do you mean?', opts))
        else:
            raise GoalNeedsInput(Clarification("recipient", f'I could not find a contact matching "{recipient}" on the page. '
                                                            "Which email address should I use?"))
        tokens = self._recipient_tokens(recipient, matches, distinct)
        shown = await self._await_committed(cand, tokens, recipient)
        if not any(shown) and kind == "address":
            await self._commit_with_key(goal, cand, "Enter")          # one more way to turn a typed address into a recipient
            shown = await self._await_committed(cand, tokens, recipient)
        if not any(shown):
            raise GoalFailed("the recipient is not committed in the composer (a typed address or an un-selected suggestion is not a recipient)")
        self.rec(ev.VERIFICATION, goal, scope="recipient_committed", result="complete", reason="the composer holds the recipient as a committed entry")
        # the value the composer really holds: the resolved address when the page showed one
        return next((t for t, ok in zip(tokens, shown) if ok and "@" in t), next((t for t, ok in zip(tokens, shown) if ok), recipient))

    async def _recipient_state(self, cand: Candidate, tokens: list[str], typed: str | None = None) -> list[bool]:
        """Per token: is it a COMMITTED recipient?  Chip / hidden to-value / email attribute / (consumed field + chip inside a
        list); a plain input without autocomplete counts by its value.  A combobox that still holds the text does not."""
        el = cand.element
        from agent.field_ranker import _normalise_xpath
        try:
            st = await self.page.evaluate(RECIPIENT_STATE_JS, {"xpath": _normalise_xpath(el.get("xpath")), "placeholder": el.get("placeholder") or None,
                                                              "label": el.get("aria_label") or None, "tokens": [str(t) for t in tokens]})
        except Exception:
            return [False] * len(tokens)
        out = []
        for tok, p in zip(tokens, st.get("per") or []):
            t = " ".join(str(tok).lower().split())
            # "consumed": the control turned what was typed into something else (a chip) and cleared itself.  Judged against
            # the TYPED text (the token may be a resolved address that was never in the field); with no typed text, an empty field.
            val = str(st.get("value") or "")
            consumed = bool(st.get("found")) and not st.get("expanded") and (
                (" ".join(typed.lower().split()) not in val) if typed else val == "")
            plain = bool(st.get("found")) and not st.get("isCombo") and t in str(st.get("value") or "") and not st.get("expanded")
            out.append(bool(p.get("attr") or p.get("hidden") or p.get("outside") or (consumed and p.get("inl")) or plain))
        return out + [False] * (len(tokens) - len(out))

    async def _await_committed(self, cand: Candidate, tokens: list[str], typed: str | None = None, wait_s: float = 1.6) -> list[bool]:
        deadline = time.monotonic() + wait_s
        while True:
            shown = await self._recipient_state(cand, tokens, typed)
            if any(shown) or time.monotonic() >= deadline:
                return shown
            await asyncio.sleep(0.25)

    @staticmethod
    def _recipient_tokens(recipient: str, matches: list[dict], distinct: dict) -> list[str]:
        if len(distinct) == 1:
            # a NAME was typed and one contact was chosen: the composer must show THAT contact, not the typed fragment
            t = _suggestion_text(next(iter(distinct.values())))
            m = _EMAIL_ADDR.search(t)
            toks = ([m.group(0)] if m else []) + [t.split("<")[0].strip()]
            return [x for x in toks if x]
        return [recipient]

    async def _click_suggestion(self, goal: Goal, el: dict) -> None:
        from agent.goal_runner import GoalFailed
        r = await self._exec(goal, Action(type=ActionType.CLICK, target=_interactive_target(el, fallback_role="option"),
                                          reason=f"Select the contact '{_suggestion_text(el)[:50]}'"))
        if not r["ok"]:
            raise GoalFailed(f"could not select the suggested contact: {r['error']}")

    async def _commit_with_key(self, goal: Goal, cand: Candidate, key: str = "Tab") -> None:
        tgt = target_dict(cand, [cand])
        await self._exec(goal, Action(type=ActionType.PRESS, key=key, target=Target(**tgt) if tgt else None,
                                      reason="Confirm the typed address as a recipient"))

    # ---------------------------------------------------------------- COMPOSE
    async def _g_compose(self, goal: Goal) -> None:
        from agent.goal_runner import GoalFailed
        p = goal.params
        recipient = (p.get("recipient") or "").strip()
        rk = classify_recipient(recipient)
        if rk in ("none", "relationship"):
            raise GoalNeedsInput(Clarification("recipient", "Which email address or contact name should I send this to?" if rk == "none"
                                               else f'Which email address or contact name is "{recipient}"?'))
        body = (p.get("body") or "").strip()
        if not body:
            body = await self._author_body(goal)
            p["body"] = body
        subject = (p.get("subject") or "").strip()
        ctx = await self._open_composer(goal)
        resolved = await self._resolve_recipient(goal, ctx, recipient)
        ctx, _, _ = await self._observe()
        if subject:
            await self._type_field(goal, ctx, SUBJECT, "subject", subject, "subject")
            ctx, _, _ = await self._observe()
        body_cand = await self._type_field(goal, ctx, BODY, "message body", body, "message body")
        # read EVERYTHING back from the live composer: recipient still committed, body in the BODY editor exactly once
        # (not in the search box or twice), subject in the SUBJECT field
        chk = await verify_in_field(self.page, body_cand, body)
        body_ok = bool(chk.get("ok")) and int(chk.get("count") or 0) == 1
        subj_ok = True
        if subject:
            sc = rank_fields(await self._fresh_ctx(), SUBJECT, "subject", allow_weak_fallback=False).best()
            subj_ok = bool(sc and (await verify_in_field(self.page, sc, subject)).get("ok"))
        ctx2 = await self._fresh_ctx()
        rc = rank_fields(ctx2, RECIPIENT, "recipient", allow_weak_fallback=False).best()
        rcpt_ok = bool(rc and any(await self._recipient_state(rc, [resolved]))) if rc else any(await self._committed_contains(resolved))
        ok = body_ok and subj_ok and rcpt_ok
        stage = ("recipient not committed" if not rcpt_ok else "subject missing" if not subj_ok else
                 "message body missing or typed more than once" if not body_ok else "")
        self.rec(ev.VERIFICATION, goal, scope="compose_fields", result="complete" if ok else "incomplete",
                 recipient_committed=rcpt_ok, subject_ok=subj_ok, body_ok=body_ok, body_occurrences=chk.get("count"), failed_stage=stage or None)
        if not self._verdict(goal, Verdict(ok, "the composer shows the committed recipient" + (", subject" if subject else "") +
                                           " and body exactly as typed; nothing was sent" if ok else f"draft not verified: {stage}")):
            raise GoalFailed(f"the composed draft could not be verified ({stage})")
        goal.evidence["compose"] = {"recipient": resolved, "subject": subject or None, "body": body, "sent": False,
                                    "subject_blank": not subject}

    async def _fresh_ctx(self) -> dict:
        ctx, _, _ = await self._observe()
        return ctx

    async def _author_body(self, goal: Goal) -> str:
        """Write a body from a stated purpose with ONE bounded AI call; otherwise ask the user. Never invents silently."""
        from agent.ai_assist import ask_json
        from agent.adaptive_planner import PlannerUnavailable
        from agent.understanding import UnderstandingError, build_body_prompt, validate_body
        p = goal.params
        purpose = p.get("purpose") or ""
        if purpose:
            try:
                data, provider = await ask_json(
                    prompt=build_body_prompt(purpose, p.get("recipient"), p.get("channel", "message")), purpose="compose_body",
                    providers=self.providers, order=list(getattr(self.rt, "order", [])), health=self.rt.health,
                    budget=self.rt.budget, recorder=self.rt.recorder)
                body = validate_body(data, purpose)
                self.rec(ev.UNDERSTANDING, goal, authored_body=True, provider=provider, chars=len(body))
                return body
            except (PlannerUnavailable, UnderstandingError) as exc:
                self.rec(ev.RECOVERY, goal, level=6, strategy="body_needs_user", reason=str(exc)[:160])
        raise GoalNeedsInput(Clarification("body", "What exactly should the " + p.get("channel", "message") + " say?"))

    # ---------------------------------------------------------------- SEND
    def _compose_info(self) -> dict:
        for g in self.state.goals:
            if g.kind == GoalKind.COMPOSE and g.evidence.get("compose"):
                return g.evidence["compose"]
        return {}

    async def _g_send(self, goal: Goal) -> None:
        from agent.goal_runner import GoalFailed, _find_by_text
        if any(g.kind == GoalKind.COMPOSE and g.params.get("no_send") for g in self.state.goals):
            raise GoalFailed("the instruction said not to send, so nothing was sent")
        info = self._compose_info()
        to = info.get("recipient")
        if goal.id not in self.approved:
            desc = "send the email" + (f" to {to}" if to else "") + (f' with subject "{info["subject"]}"' if info.get("subject") else "")
            req = ConfirmationRequest(SEND, desc, "Send", goal_index=goal.id)
            self.rec(ev.CONFIRMATION_REQUESTED, goal, kind=SEND, description=desc)
            raise GoalNeedsConfirmation(req)
        ctx, before, _ = await self._observe()
        btn = _find_by_text(ctx, r"^send(?:\s+(?:now|message|email|e-?mail))?$|^send", roles=("button", "link", "menuitem"))
        if btn is None:
            raise GoalFailed("no Send button was found in the open message")
        r = await self._exec(goal, Action(type=ActionType.CLICK, target=_interactive_target(btn, fallback_role="button"),
                                          reason="Send the message (confirmed by the user)"))
        if not r["ok"]:
            raise GoalFailed(f"clicking Send failed: {r['error']}")
        await asyncio.sleep(0.6)
        ctx2, after, _ = await self._observe()
        still_open = self._composer_ready(ctx2)
        said_sent = bool(re.search(r"\b(?:message|email|mail)\s+sent\b|\bsent\b", str(ctx2.get("body_text") or "")[:3000], re.I))
        ok = (not still_open) or said_sent
        if not self._verdict(goal, Verdict(ok, "the composer closed / the page reports the message as sent" if ok
                                           else "the composer is still open and no 'sent' confirmation appeared")):
            raise GoalFailed("could not confirm that the message was sent")
        goal.evidence["sent"] = True
