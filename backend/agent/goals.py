"""Natural-language task decomposition into ORDERED GOALS.

    "Open Google, search for A, then go back and search for B"
        -> NAVIGATE(google) , SEARCH(A) , BACK , SEARCH(B)

Why this exists: treating the whole sentence as one blob made the search query
absorb control language ("Python multithreading tutorials, then go back and
search for ...").  Here every clause becomes exactly one goal with clean
parameters, so control words can never leak into a text field.

Generic: no website names, selectors or per-site grammar.  Site NAMES are only
looked up through an injected resolver (the existing known-site table); unknown
names stay unresolved instead of being guessed.  Stdlib only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class GoalKind(str, Enum):
    NAVIGATE = "navigate"
    SEARCH = "search"
    TYPE = "type"
    CLICK = "click"
    PRESS = "press"
    SCROLL = "scroll"
    BACK = "back"
    FORWARD = "forward"
    NEW_TAB = "new_tab"
    SWITCH_TAB = "switch_tab"
    CLOSE_TAB = "close_tab"
    FIND = "find"
    EXTRACT = "extract"
    WAIT = "wait"
    FILL = "fill"                  # enter an ordinary identifier (phone / email) into the page's matching field, read back
    COMPOSE = "compose"            # write a message/email: recipient + subject + body, left as a DRAFT
    SEND = "send"                  # consequential: only ever executed after the user's explicit confirmation
    ADAPTIVE = "adaptive"          # a clause no deterministic rule understands: AI is scoped to THIS clause


class GoalStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


@dataclass
class Goal:
    id: int
    kind: GoalKind
    text: str                                   # the original clause
    params: dict = field(default_factory=dict)
    status: GoalStatus = GoalStatus.PENDING
    attempts: int = 0
    note: str = ""
    evidence: dict = field(default_factory=dict)

    def label(self) -> str:
        p = self.params
        k = self.kind
        if k == GoalKind.SEARCH:
            return f'search "{p.get("query")}"' + (f' on {p["site"]}' if p.get("site") else "")
        if k == GoalKind.TYPE:
            return f'type "{p.get("text")}"'
        if k == GoalKind.FILL:
            if p.get("secret"):
                return f'fill the {p.get("field")} field (never automated)'
            return f'fill the {p.get("field")} field with {mask_value(p.get("value"))}'
        if k == GoalKind.NAVIGATE:
            return f'open {p.get("url") or p.get("site")}'
        if k == GoalKind.NEW_TAB:
            return f'open {p.get("url") or p.get("site") or "blank page"} in a new tab'
        if k == GoalKind.CLICK:
            if p.get("ordinal"):
                return f'open the {p["ordinal"]} {p.get("what") or "result"}' + (" in a new tab" if p.get("new_tab") else "")
            return f'click "{p.get("target")}"'
        if k == GoalKind.PRESS:
            return f'press {p.get("key")}'
        if k == GoalKind.SCROLL:
            return f'scroll {p.get("direction", "down")}' + (f' to the {p["to"]}' if p.get("to") else "")
        if k == GoalKind.SWITCH_TAB:
            return f'switch to the {p.get("which") or "other"} tab'
        if k == GoalKind.COMPOSE:
            return f'compose {p.get("channel") or "message"}' + (f' to {p["recipient"]}' if p.get("recipient") else "") + " (draft)"
        if k == GoalKind.SEND:
            return f'send {p.get("what") or "the message"} (needs confirmation)'
        if k == GoalKind.FIND:
            return f'find "{p.get("target")}"'
        return f"{k.value}: {self.text}"

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind.value, "text": self.text, "label": self.label(),
                "status": self.status.value, "params": redact_params({k: v for k, v in self.params.items() if not k.startswith("_")})}



# --------------------------------------------------------------------------
# ordinary identifiers vs. secrets (generic: a field KIND, never a website)
# --------------------------------------------------------------------------
_PHONE_NOUN = r"(?:mobile|phone|cell|contact|telephone)(?:\s+(?:number|no\.?))?"
_EMAIL_NOUN = r"e-?mail(?:\s+(?:address|id))?"
_SECRET_NOUN = (r"(?:password|pass\s?code|passcode|otp|one[\s-]time\s+(?:password|code|passcode)|verification\s+code|"
                r"security\s+code|pin|cvv|card\s+number)")
_FIELD_NOUN = rf"(?P<field>{_PHONE_NOUN}|{_EMAIL_NOUN}|{_SECRET_NOUN})"
_FILL = re.compile(
    rf"^(?:fill(?:\s+in|\s+out)?|enter|type|input|put|provide|add|write|key\s+in)\s+(?:in\s+)?(?:(?:the|my|a|their|your)\s+)?{_FIELD_NOUN}"
    rf"(?:\s+(?:field|box|input))?\s*(?:as|with|to|is|=|:|being|of)?\s*(?P<val>.*)$", re.I)
_PHONE_VAL = re.compile(r"\+?\d[\d\s().-]{5,}\d")
_EMAIL_VAL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def field_class(text: str) -> str | None:
    """'phone' | 'email' | 'secret' | None for a field description such as "the phone number field"."""
    t = (text or "").lower()
    if re.search(rf"\b{_SECRET_NOUN}\b", t):
        return "secret"
    if re.search(rf"\b{_EMAIL_NOUN}", t):
        return "email"
    if re.search(rf"\b{_PHONE_NOUN}", t):
        return "phone"
    return None


def mask_value(v: object) -> str:
    s = str(v or "")
    return ("*" * max(0, len(s) - 4) + s[-4:]) if len(s) > 4 else ("*" * len(s))


def redact_params(params: dict) -> dict:
    """Display / persistence copy: identifier values are masked, secret values never present."""
    out = dict(params)
    if "value" in out and out.get("field") in ("phone", "email") and out.get("value"):
        out["value"] = mask_value(out["value"])
    return out


def parse_fill(c: str, g) -> "Goal | None":
    m = _FILL.match(c)
    if not m:
        return None
    fld = field_class(m.group("field"))
    if fld is None:
        return None
    val = " ".join(m.group("val").split()).strip(" .,;!?\"'\u201c\u201d")
    if fld == "secret":
        kind = "otp" if re.search(r"otp|one|verification|security", m.group("field"), re.I) else "password"
        sg = g(GoalKind.FILL, field=kind, secret=True, target=m.group("field").lower())
        sg.text = f"fill the {kind} field (value withheld)"        # the secret itself never travels in goal text
        return sg
    rx = _PHONE_VAL if fld == "phone" else _EMAIL_VAL
    vm = rx.search(val)
    value = vm.group(0).strip() if vm else ""
    fg = g(GoalKind.FILL, field=fld, value=value, target=m.group("field").lower())
    fg.text = f"fill the {fld} field"                   # the value lives in params only (masked in every display copy)
    return fg

# --------------------------------------------------------------------------
# segmentation
# --------------------------------------------------------------------------
_VERBS = (
    r"(?:open|go|goto|visit|navigate|click|press|hit|type|enter|write|input|search|look|find|locate|scroll|select|choose|"
    r"close|switch|return|come|tap|extract|read|tell|give|show|wait|refresh|reload|download|play|expand|focus|back|forward|launch|load|compose|draft|prepare|create|send|"
    r"say|ask|fill|submit|report|summari[sz]e)"
)
_SPLIT = re.compile(
    rf"\s*(?:;"
    rf"|,\s*(?:and\s+)?(?:then\s+)?(?={_VERBS}\b)"
    rf"|\band\s+then\b,?"
    rf"|\bthen\b,?"
    rf"|\bnext\b,?"
    rf"|\bafter\s+that\b,?"
    rf"|\bafterwards\b,?"
    rf"|\bfollowed\s+by\b"
    rf"|\bsubsequently\b,?"
    rf"|\bfinally\b,?"
    rf"|\band\s+(?={_VERBS}\b)"
    rf"|\s+(?=(?:open|click(?:\s+on)?)\s+(?:the\s+)?(?:first|second|third|1st|2nd|3rd|top)\b)"
    rf")\s*",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s,;]+", re.IGNORECASE)
_BODY_LITERAL = re.compile(
    r"(\b(?:saying|that\s+says|which\s+says|with\s+(?:the\s+)?(?:message|body|text)(?:\s*[:,])?)\s+)"
    r"((?!\x01)[^;]+?)(?=\s*(?:[,;]\s*)?(?:\bthen\b|\band\s+then\b|\bafter\s+that\b)|\s*[.!]?\s*$)", re.I)
_QUOTE_SPAN = re.compile(r"\"[^\"]+\"|\u201c[^\u201d]+\u201d|(?<![\w])'[^']+'(?![\w])")
_LEAD = re.compile(r"^(?:please\s+|now\s+|also\s+|and\s+|then\s+|just\s+|simply\s+)+", re.IGNORECASE)


# conversational wrapping that carries no task content: "Hey NEXA, could you ... for me, please?"
_POLITE_LEAD = re.compile(
    r"^(?:(?:hey|hi|hello|ok(?:ay)?|so|well|alright|nexa|assistant)[\s,!]+)*"
    r"(?:(?:can|could|would|will)\s+you(?:\s+(?:please|kindly))?\s+|(?:please|kindly)\s+|"
    r"i\s+(?:want|need|would\s+like|'d\s+like)\s+you\s+to\s+|(?:i\s+)?(?:want|need)\s+you\s+to\s+|"
    r"(?:go\s+ahead\s+and|just)\s+)*", re.I)
_POLITE_TAIL = re.compile(r"(?:[\s,]+(?:for\s+me|please|kindly|thanks|thank\s+you|if\s+you\s+can|now))+\s*[?.!]*\s*$", re.I)


_SPOKEN_DOT = re.compile(r"(?<=\w)\s+dot\s+(com|org|net|io|in|co|edu|gov|ai)\b", re.I)


def strip_politeness(text: str) -> str:
    t = _SPOKEN_DOT.sub(r".\1", " ".join(str(text or "").split()))     # voice transcripts: "<name> dot com"
    for _ in range(3):
        n = _POLITE_TAIL.sub("", _POLITE_LEAD.sub("", t)).strip()
        if n == t or not n:
            break
        t = n
    return t.rstrip("?") if t.endswith("?") and not re.search(r"\b(?:what|why|how|who|when|where|which)\b", t, re.I) else t


def _clean(s: str) -> str:
    s = " ".join(str(s or "").split()).strip()
    s = s.strip(" ,.;:!")
    if len(s) >= 2 and s[0] in "\"'“‘" and s[-1] in "\"'”’":
        s = s[1:-1].strip()
    return s.strip(" ,.;:!")


_TYPE_TARGET = re.compile(
    r"\s+(?:into|in|on|inside)\s+(?:the\s+|a\s+|an\s+)?(?P<tgt>(?:[\w-]+\s+){0,3}?(?:field|box|input|textbox|editor|area|bar|textarea|composer|prompt))\b.*$", re.I)
_TEXT_NOUN = re.compile(r"^(?:the\s+)?(?:exact\s+)?(?:text|string|phrase|words?|message)\s*:?\s+(?=[\"'\u201c\x01])", re.I)


def split_type_clause(rest: str) -> tuple[str, str]:
    """``rest`` is what follows the verb of a typing clause.  Returns (literal text, target description).

    Quoted text is taken verbatim (commas and control words inside stay).  Unquoted text ends where an
    "into/in/on the <thing> field|box|editor..." target description starts.
    """
    rest = " ".join(str(rest or "").split())
    rest = _TEXT_NOUN.sub("", rest)
    q = _QUOTE_SPAN.search(rest)
    if q and not rest[:q.start()].strip():
        text = q.group(0)[1:-1]
        tail = rest[q.end():]
        tm = re.match(r"\s*(?:into|in|on|inside)\s+(?:the\s+|a\s+|an\s+)?(?P<t>.+?)\s*[.!]?$", tail, re.I)
        return text.strip(), (tm.group("t").strip() if tm else "")
    tm = _TYPE_TARGET.search(rest) or _TARGET_SUFFIX.search(rest)
    target = ""
    if tm:
        target = (tm.groupdict().get("tgt") or re.sub(r"^\s+(?:into|in|on)\s+(?:the\s+)?", "", tm.group(0), flags=re.I)).strip(" .")
        rest = rest[:tm.start()]
    return _clean(rest), target


def split_clauses(instruction: str) -> list[str]:
    # protect URLs from splitting (they may contain commas/semicolons in rare cases)
    urls: list[str] = []

    def keep(m: re.Match) -> str:
        urls.append(m.group(0).rstrip(".,;)"))
        return f"\x00{len(urls) - 1}\x00"

    text = _URL.sub(keep, strip_politeness(instruction))
    # quoted literals ("type 'a, then b'") are data, never control words: protect them from splitting
    quotes: list[str] = []

    def keep_quote(m: re.Match) -> str:
        quotes.append(m.group(0))
        return f"\x01{len(quotes) - 1}\x01"

    text = _QUOTE_SPAN.sub(keep_quote, text)
    # free text after "saying / that says / with the message" is the literal body of a message: it runs to the end
    # of the clause (a following "then <goal>" ends it) and must not be cut at its own commas / "and"
    text = _BODY_LITERAL.sub(lambda m: m.group(1) + keep_quote(re.match(r".+", m.group(2))), text)
    # "<url> , <more>" - a comma right after a URL is always a clause boundary
    text = re.sub(r"(\x00\d+\x00)\s*[,;]\s*", r"\1 ; ", text)
    out = []
    for part in _SPLIT.split(text):
        part = _LEAD.sub("", (part or "").strip())
        part = re.sub(r"\x00(\d+)\x00", lambda m: urls[int(m.group(1))], part)
        part = re.sub(r"\x01(\d+)\x01", lambda m: quotes[int(m.group(1))], part).strip(" ,.;")
        if part:
            out.append(part)
    return out


# --------------------------------------------------------------------------
# clause -> goal
# --------------------------------------------------------------------------
_KEYS = (r"enter|return|escape|esc|tab|space|backspace|delete|home|end|page\s?up|page\s?down|"
         r"arrow\s?(?:up|down|left|right)|up|down|left|right|[a-z0-9]")
_ORD = {"first": "first", "1st": "first", "top": "first", "second": "second", "2nd": "second",
        "third": "third", "3rd": "third", "fourth": "fourth", "fifth": "fifth", "last": "last"}

_BACK = re.compile(r"^(?:go\s+|navigate\s+|come\s+|head\s+|click\s+)?back(?:\s+(?:one\s+page|to\s+the\s+(?:previous|last|prior)\s+page|to\s+(?:the\s+)?(?:previous|last|prior)\b.*))?$|"
                   r"^return(?:\s+to\s+(?:the\s+)?(?:previous|last|prior|search|results?|home|start)\b.*)?$|^go\s+to\s+the\s+(?:previous|last|prior)\s+page$", re.I)
_FORWARD = re.compile(r"^(?:go\s+|navigate\s+|move\s+)?forward(?:\s+(?:one\s+page|to\s+the\s+next\s+page))?$", re.I)
_SCROLL = re.compile(r"^scroll\b(?P<rest>.*)$", re.I)
_NEWTAB_SITE = re.compile(r"^(?:open|launch|load|go\s+to|visit)\s+(?P<site>.+?)\s+in\s+(?:a\s+|another\s+|the\s+)?new\s+(?:tab|window)$", re.I)
_NEWTAB_BLANK = re.compile(r"^open\s+(?:a\s+)?new\s+(?:tab|window)(?:\s+(?:and\s+)?(?:go\s+to|with|for)\s+(?P<site>.+))?$", re.I)
_SWITCH = re.compile(r"^(?:switch|go|come|move)\s+(?:back\s+)?to\s+(?:the\s+)?(?P<which>new|newest|first|original|previous|last|other|second|next|old|earlier)?\s*(?:tab|window|page)$", re.I)
_CLOSE = re.compile(r"^close\s+(?:the\s+|this\s+|current\s+|that\s+)?(?:(?:new|current|this|other)\s+)?(?:tab|window|page)$", re.I)
_PRESS = re.compile(rf"^(?:press|hit)\s+(?:the\s+)?(?P<key>{_KEYS})(?:\s+key)?$", re.I)
_SEARCH_SITE_FOR = re.compile(r"^(?:search|look\s+up)\s+(?P<site>[\w.-]+)\s+for\s+(?P<q>.+)$", re.I)
_SEARCH = re.compile(r"^(?:search(?:\s+for|\s+up)?|look\s+up)\s+(?P<q>.+)$", re.I)
_SEARCH_ON = re.compile(r"^(?P<q>.+?)\s+(?:on|in|at|using|via)\s+(?P<site>[\w.-]+(?:\s[\w.-]+)?)$", re.I)
_TYPE = re.compile(r"^(?:type|enter|write|input|put)\s+(?:in\s+|into\s+)?(?P<text>.+)$", re.I)
_TARGET_SUFFIX = re.compile(r"\s+(?:into|in|on)\s+(?:the\s+)?(?:chat|message|input|text|prompt|search|compos\w*|box|field|textbox|bar)\b.*$", re.I)
_EXTRACT = re.compile(r"^(?:summari[sz]e|tell\s+me|give\s+me|extract|read|what\s+is|what's|show\s+me|report|return\s+the|copy|get\s+me|list)\b.*$", re.I)
_ORDINAL_CLICK = re.compile(
    r"^(?:open|click(?:\s+on)?|select|choose|tap|go\s+to|pick)\s+(?:on\s+)?(?:the\s+)?(?P<ord>first|1st|top|second|2nd|third|3rd|fourth|fifth|last)\s+"
    r"(?:relevant\s+|top\s+|best\s+|matching\s+|search\s+)*(?P<what>[\w-]+(?:\s[\w-]+)?)(?P<tail>.*)$", re.I)
_NAV = re.compile(r"^(?:open|go\s+to|go\s+on|goto|visit|navigate\s+to|launch|load|browse\s+to)\s+(?P<obj>.+)$", re.I)
_CLICK = re.compile(r"^(?:click(?:\s+on)?|tap|press|select|choose|hit)\s+(?P<obj>.+)$", re.I)
_FIND = re.compile(r"^(?:find|locate|look\s+for)\s+(?P<target>.+)$", re.I)
_ASSISTANT = r"(?:it|chat\s?gpt|gemini|claude|grok|copilot|the\s+(?:bot|assistant|chat|ai))"
_SAY = re.compile(
    rf"^(?:(?P<say>say)|(?P<tell>tell)\s+{_ASSISTANT}|(?P<ask>ask)\s+{_ASSISTANT})\s+(?P<text>.+)$", re.I)
_RESULTISH = re.compile(
    r"^(?:a|an|the|any|some|one)?\s*(?:good|relevant|useful|best|top|nice|suitable|decent|most\s+relevant|first|helpful)?\s*"
    r"(?P<what>result|link|video|article|page|repository|repo|project|course|tutorial|answer)s?$", re.I)
_WAIT = re.compile(r"^wait(?:\s+(?:for\s+)?(?P<n>\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)?)?\b.*$", re.I)
_DOMAIN = re.compile(r"^(?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:/\S*)?$", re.I)

SiteResolver = Callable[[str], Optional[str]]


def _site_url(obj: str, resolver: Optional[SiteResolver]) -> tuple[Optional[str], Optional[str]]:
    """(url, site_name). Only returns a URL for explicit URLs, domains and KNOWN site names."""
    obj = _clean(re.sub(r"^(?:the\s+|a\s+|an\s+|my\s+|our\s+)", "", obj, flags=re.I))
    m = _URL.search(obj)
    if m:
        return m.group(0).rstrip(".,;)"), None
    if _DOMAIN.match(obj):
        return "https://" + obj.removeprefix("https://").removeprefix("http://"), None
    if resolver is not None:
        name = re.sub(r"\b(?:website|site|web\s?page|homepage|home\s?page|app)\b", "", obj, flags=re.I).strip()
        url = resolver(name.lower())
        if url:
            return url, name
    return None, None


_COMPOSE = re.compile(
    r"^(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?(?:please\s+)?(?:quickly\s+)?"
    r"(?P<verb>compose|draft|write|create|prepare|start)\s+(?:me\s+)?(?:a|an|the|new|my)?\s*"
    r"(?:(?:quick|short|brief|new|polite|simple|formal)\s+)*(?P<chan>e-?mail|mail|message|note)\b(?P<rest>.*)$", re.I)
_NO_SEND = re.compile(r"(?:,?\s*(?:but|and|just|only)?\s*)(?:do\s*n[o']?t|don't|never|without|not)\s+(?:to\s+)?send(?:ing)?(?:\s+(?:it|this|that|the\s+\w+))?(?:\s+(?:yet|now))?", re.I)
_SUBJECT = re.compile(r"\b(?:with\s+(?:the\s+)?)?subject(?:\s+line)?\s*(?:[:=]|of|as|is)?\s*(?P<s>\x01\d+\x01|\"[^\"]+\"|'[^']+'|.+?)(?=\s+(?:saying|that\s+says|with\s+(?:the\s+)?(?:message|body|text))\b|$)", re.I)
_BODY = re.compile(r"\b(?:saying|that\s+says|which\s+says|with\s+(?:the\s+)?(?:message|body|text)(?:\s*[:,])?)\s+(?P<b>.+)$", re.I)
_PURPOSE = re.compile(r"\b(?:explaining|asking|telling|informing|requesting|regarding|about|to\s+tell|to\s+ask|to\s+let)\b\s*(?P<p>.+)$", re.I)
_TO = re.compile(r"\bto\s+(?P<to>.+?)(?=\s+(?:saying|that\s+says|which\s+says|with|about|explaining|asking|telling|informing|requesting|regarding)\b|$)", re.I)
_SEND = re.compile(r"^(?:(?:now|then|go\s+ahead\s+and|please)\s+)*send\s+(?:this|that|the|my|it)(?:\s+(?P<what>e-?mail|mail|message))?(?:\s+now)?$", re.I)
_EMAIL_ADDR = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _lit(text: str) -> str:
    t = " ".join(str(text or "").split()).strip(" ,.;?!")
    if len(t) >= 2 and t[0] in "\"'\u201c" and t[-1] in "\"'\u201d":
        t = t[1:-1].strip()
    return t


def parse_compose(c: str) -> dict | None:
    """Entities of "compose an email to X [with subject S] saying B [but do not send it]". None if not a compose clause."""
    m = _COMPOSE.match(c)
    if not m:
        return None
    rest = m.group("rest")
    no_send = bool(_NO_SEND.search(rest))
    rest = _NO_SEND.sub("", rest).strip()
    params: dict = {"channel": "email" if "mail" in m.group("chan").lower() else m.group("chan").lower(),
                    "send": False, "no_send": no_send}
    sm = _SUBJECT.search(rest)
    if sm:
        params["subject"] = _lit(sm.group("s"))
        rest = (rest[:sm.start()] + " " + rest[sm.end():]).strip()
    bm = _BODY.search(rest)
    if bm:
        params["body"] = _lit(bm.group("b"))
        rest = rest[:bm.start()].strip()
    else:
        pm = _PURPOSE.search(rest)
        if pm:
            params["purpose"] = _lit(pm.group("p"))
            rest = rest[:pm.start()].strip()
    tm = _TO.search(rest)
    if tm:
        params["recipient"] = _lit(tm.group("to"))
    return params


def parse_clause(clause: str, goal_id: int, resolver: Optional[SiteResolver] = None) -> Goal:
    raw = clause
    c = _clean(clause)
    low = c.lower()

    def g(kind: GoalKind, **params) -> Goal:
        return Goal(id=goal_id, kind=kind, text=raw, params={k: v for k, v in params.items() if v is not None})

    if _BACK.match(c):
        return g(GoalKind.BACK)
    if _FORWARD.match(c):
        return g(GoalKind.FORWARD)

    sm_ = _SEND.match(c)
    if sm_:
        return g(GoalKind.SEND, what=(sm_.group("what") or "message").lower())
    cp = parse_compose(c)
    if cp is not None:
        return g(GoalKind.COMPOSE, **cp)

    # "say hello" / "tell it hello" / "ask it <question>": type the words into the page's message box and SUBMIT.
    # The user's verb is the explicit request to send, so decompose() expands this into TYPE + PRESS Enter.
    sy = _SAY.match(c)
    if sy:
        said = _clean(re.sub(r"\s+to\s+(?:it|chat\s?gpt|gemini|claude|grok|the\s+\w+)$", "", sy.group("text"), flags=re.I))
        if sy.group("ask"):
            said = re.sub(r"^to\s+", "", said, flags=re.I)
        if said:
            return g(GoalKind.TYPE, text=said, submit=True)

    # "open the second result in a new tab" is an ordinal CLICK, not a site name
    mo = _ORDINAL_CLICK.match(c)
    if mo and re.search(r"new\s+(?:tab|window)", low):
        what = re.sub(r"\b(?:in|on)\s+(?:a\s+)?new\s+(?:tab|window)\b", "", mo.group("what"), flags=re.I).strip()
        what = re.split(r"\s+(?:in|on|from|for|of|under)\b", what, maxsplit=1)[0].strip() or "result"
        return g(GoalKind.CLICK, ordinal=_ORD[mo.group("ord").lower()], what=what, new_tab=True)

    m = _NEWTAB_SITE.match(c)
    if m:
        url, site = _site_url(m.group("site"), resolver)
        return g(GoalKind.NEW_TAB, url=url, site=site or (None if url else _clean(m.group("site"))))
    m = _NEWTAB_BLANK.match(c)
    if m:
        url, site = _site_url(m.group("site") or "", resolver) if m.group("site") else (None, None)
        return g(GoalKind.NEW_TAB, url=url, site=site)

    m = _SWITCH.match(c)
    if m:
        return g(GoalKind.SWITCH_TAB, which=(m.group("which") or "other").lower())
    if _CLOSE.match(c):
        return g(GoalKind.CLOSE_TAB)

    m = _SCROLL.match(c)
    if m:
        rest = m.group("rest").lower()
        direction = "up" if re.search(r"\bup\b|\btop\b", rest) else "down"
        to = "bottom" if re.search(r"\b(?:bottom|end)\b", rest) else ("top" if re.search(r"\btop\b", rest) else None)
        return g(GoalKind.SCROLL, direction=direction, to=to)

    m = _PRESS.match(c)
    if m:
        key = re.sub(r"\s+", "", m.group("key"))
        norm = {"esc": "Escape", "escape": "Escape", "enter": "Enter", "return": "Enter", "tab": "Tab", "space": "Space",
                "backspace": "Backspace", "delete": "Delete", "home": "Home", "end": "End", "pageup": "PageUp",
                "pagedown": "PageDown", "arrowup": "ArrowUp", "arrowdown": "ArrowDown", "arrowleft": "ArrowLeft",
                "arrowright": "ArrowRight", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight"}
        return g(GoalKind.PRESS, key=norm.get(key.lower(), key))

    m = _SEARCH_SITE_FOR.match(c)
    if m and resolver is not None and resolver(m.group("site").lower()):
        return g(GoalKind.SEARCH, query=_clean(m.group("q")), site=m.group("site").lower())
    m = _SEARCH.match(c)
    if m:
        q, site = _clean(m.group("q")), None
        mo = _SEARCH_ON.match(q)
        if mo and resolver is not None and resolver(mo.group("site").strip().lower()):
            q, site = _clean(mo.group("q")), mo.group("site").strip().lower()
        if q:
            return g(GoalKind.SEARCH, query=q, site=site)

    m = _ORDINAL_CLICK.match(c)
    if m:
        tail = m.group("tail") or ""
        what = re.sub(r"\b(?:in|on)\s+(?:a\s+)?new\s+(?:tab|window)\b", "", m.group("what"), flags=re.I).strip()
        what = re.split(r"\s+(?:in|on|from|for|of|under)\b", what, maxsplit=1)[0].strip() or "result"
        return g(GoalKind.CLICK, ordinal=_ORD[m.group("ord").lower()], what=what,
                 new_tab=bool(re.search(r"new\s+(?:tab|window)", low)))

    m = _EXTRACT.match(c)
    if m:
        return g(GoalKind.EXTRACT, request=c)

    fill = parse_fill(c, g)
    if fill is not None:
        return fill

    m = _TYPE.match(c)
    if m:
        text, target = split_type_clause(m.group("text"))
        fc = field_class(target) if target else None
        if text and fc in ("phone", "email") and (_PHONE_VAL if fc == "phone" else _EMAIL_VAL).fullmatch(text.strip()):
            fg = g(GoalKind.FILL, field=fc, value=text.strip(), target=target)
            fg.text = f"fill the {fc} field"
            return fg
        if text and fc == "secret":
            kind = "otp" if re.search(r"otp|code", target, re.I) else "password"
            sg = g(GoalKind.FILL, field=kind, secret=True, target=target)
            sg.text = f"fill the {kind} field (value withheld)"
            return sg
        if text:
            return g(GoalKind.TYPE, text=text, target=target) if target else g(GoalKind.TYPE, text=text)

    m = _WAIT.match(c)
    if m:
        return g(GoalKind.WAIT, seconds=min(10.0, float(m.group("n") or 2)))

    m = _NAV.match(c)
    if m:
        obj = m.group("obj")
        url, site = _site_url(obj, resolver)
        if url:
            return g(GoalKind.NAVIGATE, url=url, site=site)
        # "open a good result / the best video": an ordinal pick of a search result, not a website
        rm = _RESULTISH.match(_clean(obj))
        if rm:
            return g(GoalKind.CLICK, ordinal="first", what=rm.group("what").lower())
        target = _clean(re.sub(r"^(?:the\s+)", "", obj, flags=re.I))
        from agent.planner import looks_like_sentence      # one shared definition of "this is a sentence, not a name"
        if looks_like_sentence(target) and not _DOMAIN.match(target):
            return g(GoalKind.ADAPTIVE)                   # a sentence is never a destination: let the planner read it
        return g(GoalKind.CLICK, target=target, maybe_site=True)

    m = _CLICK.match(c)
    if m:
        obj = _clean(re.sub(r"^(?:the\s+)", "", m.group("obj"), flags=re.I))
        obj = re.sub(r"\s+(?:link|button|tab|icon)$", "", obj, flags=re.I) or obj
        return g(GoalKind.CLICK, target=obj,
                 new_tab=bool(re.search(r"new\s+(?:tab|window)", low)))

    m = _FIND.match(c)
    if m:
        tgt = _clean(re.sub(r"^(?:the|a|an)\s+", "", _clean(m.group("target")), flags=re.I))
        tgt = re.sub(r"\s+(?:button|link|tab|icon|section|field|box|element|option|heading|text|label|menu\s+item)$", "", tgt, flags=re.I) or tgt
        return g(GoalKind.FIND, target=tgt)

    return g(GoalKind.ADAPTIVE)


def _split_site_lead(clause: str, resolver: SiteResolver) -> tuple[str, str, str] | None:
    """"open wikipedia search databases ..." -> (url, "wikipedia", "search databases ...") when a KNOWN site name
    directly follows the open-verb and an action verb follows it.  Never guesses an unknown name."""
    m = _NAV.match(_clean(clause))
    if not m:
        return None
    words = _clean(m.group("obj")).split()
    for k in (1, 2):
        if len(words) <= k:
            break
        name = " ".join(words[:k]).lower()
        url = resolver(name)
        if url and re.match(rf"^{_VERBS}\b", words[k], re.I):
            return url, name, " ".join(words[k:])
    return None


_NO_SEND_CLAUSE = re.compile(
    r"^(?:(?:but|and|also|please|just|only)\s+)*(?:do\s*n[o']?t|don't|never)\s+(?:to\s+)?send(?:ing)?(?:\s+(?:it|this|that|the\s+\w+))?(?:\s+(?:yet|now))?$", re.I)
_SUBJECT_CLAUSE = re.compile(r"^(?:(?:with|and)\s+)?(?:the\s+)?subject(?:\s+line)?\s*(?:[:=]|of|as|is)?\s*(?P<s>.+)$", re.I)
_BODY_TARGETS = {"body", "message", "message body", "the body", "the message", "email body", "the email body"}


def _absorb_compose_followup(compose: Goal, clause: str, goal: Goal) -> bool:
    """A clause right after "compose an email to X" that only completes THAT message belongs to it:
    "... and write 'Hello'" (body), "... Do not send it" (stay a draft), "... subject Hi".
    Without this the body became a stray TYPE goal, the compose goal looked body-less and the task paused to ask for text
    the user had already given.  Never absorbs anything that names another target or site."""
    p = compose.params
    c = _clean(clause)
    if _NO_SEND_CLAUSE.match(c):
        p["no_send"] = True
        return True
    # "write 'Hi.' Do not send it" is ONE clause (a full stop does not split): peel the draft-only instruction off,
    # looking only OUTSIDE quotes so a quoted body that mentions sending is never altered
    blanked = _QUOTE_SPAN.sub(lambda m: " " * len(m.group(0)), c)
    ns = _NO_SEND.search(blanked)
    if ns and goal.kind == GoalKind.TYPE:
        p["no_send"] = True
        cut = ns.end() - len(ns.group(0).lstrip(" ,"))          # the pattern's lead-in swallows spaces; cut at the phrase
        c = c[:cut].rstrip(" ,.;")
    sm = _SUBJECT_CLAUSE.match(c)
    if sm and not p.get("subject"):
        subj = _lit(sm.group("s"))
        if subj:
            p["subject"] = subj
            return True
    if goal.kind == GoalKind.TYPE and not p.get("body") and not p.get("purpose"):
        extra = {k for k in goal.params if k not in ("text", "target", "submit")}
        tgt = str(goal.params.get("target") or "").strip().lower()
        text = _lit(goal.params.get("text") or "")
        raw = re.match(r"^(?:(?:and|then|please)\s+)*(?:write|type|enter|put)\s+(?P<b>.+)$", c, re.I)
        if raw:                                   # a QUOTED body is exact text: keep its own punctuation ("...well.")
            lit = _lit(raw.group("b"))
            if len(raw.group("b").strip()) > len(lit) and raw.group("b").strip()[0] in "\"'\u201c":
                text = lit
        if text and not extra and not goal.params.get("submit") and (not tgt or tgt in _BODY_TARGETS):
            p["body"] = text
            return True
    return False


_TO_PHRASE = re.compile(r"^(?P<v>[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\s+(?:in|into)\s+(?:the\s+)?(?:to|recipients?)(?:\s+(?:field|box|line))?$", re.I)
_COMPOSE_BTN = re.compile(r"^(?:the\s+)?(?:compose|new\s+(?:message|e-?mail|mail))(?:\s+button)?$", re.I)


def _fold_stepwise_compose(goals: list[Goal]) -> list[Goal]:
    """"click Compose, enter X in To, type 'B' in the body" is ONE compose task.  Left as generic CLICK/TYPE goals the
    address was typed into whatever field had focus.  Folds only this exact shape; everything else is untouched."""
    out: list[Goal] = []
    i = 0
    while i < len(goals):
        g, nxt = goals[i], (goals[i + 1] if i + 1 < len(goals) else None)
        if (g.kind == GoalKind.CLICK and _COMPOSE_BTN.match(str(g.params.get("target") or "").strip())
                and nxt is not None and nxt.kind == GoalKind.TYPE and not nxt.params.get("target")
                and _TO_PHRASE.match(str(nxt.params.get("text") or "").strip())):
            params = {"channel": "email", "send": False, "no_send": False,
                      "recipient": _TO_PHRASE.match(str(nxt.params["text"]).strip()).group("v")}
            j = i + 2
            while j < len(goals) and goals[j].kind == GoalKind.TYPE and not goals[j].params.get("submit"):
                tgt = str(goals[j].params.get("target") or "").strip().lower()
                if tgt in _BODY_TARGETS and "body" not in params:
                    params["body"] = _lit(goals[j].params.get("text") or "")
                elif tgt == "subject" and "subject" not in params:
                    params["subject"] = _lit(goals[j].params.get("text") or "")
                else:
                    break
                j += 1
            out.append(Goal(id=len(out) + 1, kind=GoalKind.COMPOSE, text=g.text + " ; " + nxt.text, params=params))
            i = j
            continue
        g.id = len(out) + 1
        out.append(g)
        i += 1
    return out


def decompose(instruction: str, resolver: Optional[SiteResolver] = None) -> list[Goal]:
    goals = _decompose(instruction, resolver)
    return _fold_stepwise_compose(goals) if any(g.kind == GoalKind.CLICK for g in goals) else goals


def _decompose(instruction: str, resolver: Optional[SiteResolver] = None) -> list[Goal]:
    goals: list[Goal] = []
    for cl in split_clauses(instruction):
        goal = parse_clause(cl, len(goals) + 1, resolver)
        if goals and goals[-1].kind == GoalKind.COMPOSE and _absorb_compose_followup(goals[-1], cl, goal):
            continue
        if goal.kind == GoalKind.ADAPTIVE and resolver is not None:
            lead = _split_site_lead(cl, resolver)
            if lead is not None:
                url, site, rest = lead
                goals.append(Goal(id=len(goals) + 1, kind=GoalKind.NAVIGATE, text=cl, params={"url": url, "site": site}))
                for sub in split_clauses(rest) or [rest]:
                    goals.append(parse_clause(sub, len(goals) + 1, resolver))
                continue
        goals.append(goal)
        if goal.kind == GoalKind.TYPE and goal.params.pop("submit", False):
            # explicit "say/tell/ask": the message is typed, then submitted with Enter and verified separately
            goals.append(Goal(id=len(goals) + 1, kind=GoalKind.PRESS, text=f"send it ({cl})", params={"key": "Enter"}))
    return goals


# kinds the legacy single-flow planner cannot sequence safely -> the goal runner owns them
_RUNNER_KINDS = {GoalKind.FILL, GoalKind.COMPOSE, GoalKind.SEND, GoalKind.BACK, GoalKind.FORWARD, GoalKind.NEW_TAB, GoalKind.SWITCH_TAB, GoalKind.CLOSE_TAB,
                 GoalKind.SCROLL, GoalKind.PRESS, GoalKind.FIND, GoalKind.WAIT}


def needs_goal_runner(goals: list[Goal]) -> bool:
    """True when the instruction uses sequencing primitives or several searches."""
    if not goals:
        return False
    kinds = [g.kind for g in goals]
    if any(k in _RUNNER_KINDS for k in kinds):
        return True
    if sum(1 for k in kinds if k == GoalKind.SEARCH) >= 2:
        return True
    if any(g.kind == GoalKind.CLICK and g.params.get("new_tab") for g in goals):
        return True
    # typing with a described target ("into the email field", "into the editor") or typing mixed with other
    # goals needs per-goal field ranking and read-back verification
    if any(g.kind == GoalKind.TYPE and g.params.get("target") for g in goals):
        return True
    if GoalKind.TYPE in kinds and len(goals) >= 3:
        return True
    return False


def clean_search_queries(instruction: str, resolver: Optional[SiteResolver] = None) -> list[str]:
    """The text that must be typed for each search, with all control language removed."""
    return [g.params["query"] for g in decompose(instruction, resolver) if g.kind == GoalKind.SEARCH and g.params.get("query")]
