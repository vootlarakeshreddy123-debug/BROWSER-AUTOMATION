from __future__ import annotations

import re

from dataclasses import dataclass

from urllib.parse import quote_plus, urlparse

from agent.action_schema import Action, ActionPlan, ActionType, Target



# ============================================================

# SITE ADAPTER

# ============================================================

@dataclass(frozen=True)

class SiteAdapter:

    aliases: tuple[str, ...]

    url: str

    search_url_template: str | None = None



# ============================================================

# KNOWN WEBSITES

# ============================================================

SITE_ADAPTERS = [

    # Mail / calendar (entry points only; every later step is read from the live DOM)

    SiteAdapter(("gmail", "g mail", "google mail"), "https://mail.google.com"),

    SiteAdapter(("outlook", "outlook mail"), "https://outlook.live.com/mail/"),

    SiteAdapter(("google calendar", "calendar"), "https://calendar.google.com"),

    # Developer / coding

    SiteAdapter(

        ("github", "git hub"),

        "https://github.com",

        "https://github.com/search?q={query}",

    ),

    SiteAdapter(

        ("stackoverflow", "stack overflow"),

        "https://stackoverflow.com",

        "https://stackoverflow.com/search?q={query}",

    ),

    SiteAdapter(

        ("python", "python.org"),

        "https://www.python.org",

    ),

    SiteAdapter(

        ("pypi", "python package index"),

        "https://pypi.org",

        "https://pypi.org/search/?q={query}",

    ),

    SiteAdapter(

        ("npm", "npmjs"),

        "https://www.npmjs.com",

        "https://www.npmjs.com/search?q={query}",

    ),

    SiteAdapter(

        ("nodejs", "node js", "node"),

        "https://nodejs.org",

    ),

    SiteAdapter(

        ("react", "reactjs", "react js"),

        "https://react.dev",

    ),

    SiteAdapter(

        ("vite",),

        "https://vite.dev",

    ),

    SiteAdapter(

        ("vercel",),

        "https://vercel.com",

    ),

    SiteAdapter(

        ("netlify",),

        "https://www.netlify.com",

    ),

    SiteAdapter(

        ("leetcode", "leet code"),

        "https://leetcode.com",

        "https://leetcode.com/problemset/?search={query}",

    ),

    SiteAdapter(

        ("hackerrank", "hacker rank"),

        "https://www.hackerrank.com",

        "https://www.hackerrank.com/search?query={query}",

    ),

    SiteAdapter(

        ("geeksforgeeks", "geeks for geeks", "gfg"),

        "https://www.geeksforgeeks.org",

        "https://www.google.com/search?q=site%3Ageeksforgeeks.org+{query}",

    ),

    SiteAdapter(

        ("hugging face", "huggingface"),

        "https://huggingface.co",

        "https://huggingface.co/search/full-text?q={query}",

    ),

    SiteAdapter(

        ("kaggle",),

        "https://www.kaggle.com",

        "https://www.kaggle.com/search?q={query}",

    ),

    # Search / knowledge

    SiteAdapter(

        ("mdn", "mdn web docs", "mozilla developer network", "mozilla developer"),

        "https://developer.mozilla.org",

        "https://developer.mozilla.org/en-US/search?q={query}",

    ),

    SiteAdapter(

        ("google",),

        "https://www.google.com",

        "https://www.google.com/search?q={query}",

    ),

    SiteAdapter(

        ("bing",),

        "https://www.bing.com",

        "https://www.bing.com/search?q={query}",

    ),

    SiteAdapter(

        ("duckduckgo", "duck duck go"),

        "https://duckduckgo.com",

        "https://duckduckgo.com/?q={query}",

    ),

    SiteAdapter(

        ("wikipedia", "wiki"),

        "https://www.wikipedia.org",

        "https://en.wikipedia.org/wiki/Special:Search?search={query}",

    ),

    # AI

    SiteAdapter(

        ("chatgpt", "chat gpt"),

        "https://chatgpt.com",

    ),

    SiteAdapter(

        ("openai", "open ai"),

        "https://openai.com",

    ),

    # Video / social

    SiteAdapter(

        ("youtube", "you tube"),

        "https://www.youtube.com",

        "https://www.youtube.com/results?search_query={query}",

    ),

    SiteAdapter(

        ("reddit",),

        "https://www.reddit.com",

        "https://www.reddit.com/search/?q={query}",

    ),

    SiteAdapter(

        ("instagram", "insta"),

        "https://www.instagram.com",

    ),

    SiteAdapter(

        ("facebook", "fb"),

        "https://www.facebook.com",

    ),

    SiteAdapter(

        ("linkedin", "linked in"),

        "https://www.linkedin.com",

        "https://www.linkedin.com/search/results/all/?keywords={query}",

    ),

    SiteAdapter(

        ("whatsapp", "whatsapp web"),

        "https://web.whatsapp.com",

    ),

    SiteAdapter(

        ("telegram",),

        "https://web.telegram.org",

    ),

    SiteAdapter(

        ("discord",),

        "https://discord.com",

    ),

    # Shopping

    SiteAdapter(

        ("amazon india", "amazon.in", "amazon"),

        "https://www.amazon.in/",

        "https://www.amazon.in/s?k={query}",

    ),

    SiteAdapter(

        ("flipkart", "flip cart"),

        "https://www.flipkart.com/",

        "https://www.flipkart.com/search?q={query}",

    ),

    # Enterprise / cloud

    SiteAdapter(

        ("oracle cloud", "oracle cloud infrastructure", "oci"),

        "https://cloud.oracle.com/",

    ),

    SiteAdapter(

        ("oracle",),

        "https://www.oracle.com/in/",

    ),

    SiteAdapter(

        ("microsoft", "microsoft.com"),

        "https://www.microsoft.com",

    ),

    SiteAdapter(

        ("azure", "microsoft azure"),

        "https://azure.microsoft.com",

    ),

    # Learning

    SiteAdapter(

        ("w3schools", "w3 schools"),

        "https://www.w3schools.com",

        "https://www.google.com/search?q=site%3Aw3schools.com+{query}",

    ),

    SiteAdapter(

        ("coursera",),

        "https://www.coursera.org",

        "https://www.coursera.org/search?query={query}",

    ),

    SiteAdapter(

        ("udemy",),

        "https://www.udemy.com",

        "https://www.udemy.com/courses/search/?q={query}",

    ),

    SiteAdapter(

        ("canva",),

        "https://www.canva.com",

    ),

    # Entertainment

    SiteAdapter(

        ("netflix",),

        "https://www.netflix.com",

    ),

    SiteAdapter(

        ("spotify",),

        "https://open.spotify.com",

        "https://open.spotify.com/search/{query}",

    ),

    # Jobs

    SiteAdapter(

        ("indeed",),

        "https://www.indeed.com",

        "https://www.indeed.com/jobs?q={query}",

    ),

    SiteAdapter(

        ("naukri",),

        "https://www.naukri.com",

        "https://www.naukri.com/{query}-jobs",

    ),

    SiteAdapter(

        ("internshala", "intern shala"),

        "https://internshala.com",

        "https://internshala.com/internships/keywords-{query}/",

    ),

]



# ============================================================

# REGEX PATTERNS

# ============================================================

_OPEN_PREFIX_RE = re.compile(

    r"^\s*(?:open|go\s+to|visit|start|launch)\s+",

    re.IGNORECASE,

)

_OPEN_AND_SEARCH_RE = re.compile(

    r"""

    ^\s*

    (?:open|go\s+to|visit|start|launch)\s+

    (?P<site>.+?)

    \s+

    (?:and|then)\s+

    (?:search|look\s+for|find)

    \s+

    (?P<query>.+?)

    \s*$

    """,

    re.IGNORECASE | re.VERBOSE,

)

_SEARCH_ON_SITE_RE = re.compile(

    r"""

    ^\s*

    (?:search|look\s+for|find)

    \s+

    (?P<query>.+?)

    \s+

    (?:on|in)\s+

    (?P<site>.+?)

    \s*$

    """,

    re.IGNORECASE | re.VERBOSE,

)

_EXPLICIT_URL_RE = re.compile(

    r"https?://[^\s]+",

    re.IGNORECASE,

)

_DOMAIN_RE = re.compile(

    r"""

    ^

    (?:

        www\\.

    )?

    [a-zA-Z0-9-]+

    (?:

        \\.[a-zA-Z0-9-]+

    )+

    (?:

        /[^\s]*

    )?

    $

    """,

    re.IGNORECASE | re.VERBOSE,

)



# ============================================================

# TEXT NORMALIZATION

# ============================================================

def _normalise(value: str) -> str:

    value = value.lower().strip()

    value = re.sub(r"\s+", " ", value)

    return value



def _remove_terminal_punctuation(value: str) -> str:

    return value.strip().strip(".,!?;:")



def _clean_query(value: str) -> str:

    value = value.strip()

    # Search phrases commonly contain an optional leading "for".

    # Keep the actual query rather than sending "for <query>" to a

    # site's search endpoint.

    value = re.sub(

        r"^for\s+",

        "",

        value,

        flags=re.IGNORECASE,

    ).strip()

    value = re.sub(

        r"\s*,?\s*then\s+(?:open|click|go\s+to|visit)\s+"

        r"(?:the\s+)?(?:first|top|next)\b.\*$",

        "",

        value,

        flags=re.IGNORECASE,

    )

    return _remove_terminal_punctuation(value)



def _strip_open_prefix(value: str) -> str:

    return _OPEN_PREFIX_RE.sub("", value, count=1).strip()

def _try_initial_known_site_navigation(instruction: str) -> ActionPlan | None:
    """Seed only the homepage navigation for a known-site multi-step task.

    The existing SITE_ADAPTERS provide the generic site knowledge. After this
    one navigation, the adaptive planner observes the live DOM and chooses
    every subsequent action; this is not a site-specific workflow.
    """
    text = instruction.strip()
    if not text:
        return None

    match = re.match(
        r"^(?:open|go\s+to|visit|navigate\s+to)\s+(?:the\s+)?(.+?)"
        r"(?=\s*(?:,|\band\b|\bthen\b|\bfind\b|\bsearch\b|\bclick\b|\bopen\b|\bscroll\b|\blook\s+for\b|$))",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    site = _site_name_from_text(match.group(1).strip())
    if site is None:
        return None

    return _fast_plan([
        _navigate(
            site.url,
            "Open the requested website before adaptive browser reasoning.",
        )
    ])


def _has_followup_action(instruction: str) -> bool:

    """Return True when a search/open command contains another action.

    A search URL can satisfy:

        Open YouTube and search Python tutorials

    but it must not satisfy:

        Open YouTube and search Python tutorials and open the first video

    Those multi-stage commands need a real browser action plan.

    """

    return bool(

        re.search(

            r"(?:,|\s+(?:and|then))\s*(?:open|go\s+to|visit|click|find|search|look\s+for|type|write|press|fill|select|choose|create|enter|scroll|navigate)\b",

            instruction,

            flags=re.IGNORECASE,

        )

    )







# ============================================================

# SITE LOOKUP

# ============================================================

def _find_site(text: str) -> SiteAdapter | None:

    normalized = _normalise(text)

    # Exact aliases first.

    for adapter in SITE_ADAPTERS:

        for alias in adapter.aliases:

            if normalized == _normalise(alias):

                return adapter

    # Then phrase/word containment.

    # Sort aliases longest-first so "oracle cloud" wins over "oracle".

    candidates: list[tuple[int, SiteAdapter]] = []

    for adapter in SITE_ADAPTERS:

        for alias in adapter.aliases:

            alias_normalized = _normalise(alias)

            if alias_normalized in normalized:

                candidates.append(

                    (len(alias_normalized), adapter)

                )

    if not candidates:

        return None

    candidates.sort(key=lambda item: item[0], reverse=True)

    return candidates[0][1]



def _site_name_from_text(text: str) -> SiteAdapter | None:

    """

    Try to identify a known site from a phrase.

    This deliberately handles known sites before generic .com

    website fallback.

    """

    cleaned = _normalise(text)

    # Remove common page words from the end.

    cleaned = re.sub(

        r"\s+(?:homepage|home|website|site|page)$",

        "",

        cleaned,

        flags=re.IGNORECASE,

    ).strip()

    return _find_site(cleaned)



# ============================================================

# URL HELPERS

# ============================================================

def _extract_url(text: str) -> str | None:

    match = _EXPLICIT_URL_RE.search(text)

    if not match:

        return None

    url = match.group(0).rstrip(".,!?;:)")

    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:

        return None

    if not parsed.netloc:

        return None

    return url



def _looks_like_domain(text: str) -> bool:

    value = text.strip()

    if not value:

        return False

    value = value.rstrip(".,!?;:)")

    if " " in value:

        return False

    return bool(_DOMAIN_RE.match(value))



def _domain_to_url(text: str) -> str | None:

    value = text.strip().rstrip(".,!?;:)")

    if not _looks_like_domain(value):

        return None

    if value.lower().startswith(("http://", "https://")):

        return value

    return "https://" + value



# ============================================================

# ACTION BUILDERS

# ============================================================

def _navigate(

    url: str,

    reason: str = "Open the requested website.",

) -> Action:

    return Action(

        type=ActionType.NAVIGATE,

        url=url,

        reason=reason,

    )



def _click(

    *,

    name: str | None = None,

    role: str | None = None,

    css: str | None = None,

    exact: bool = False,

    reason: str = "Click the requested element.",

) -> Action:

    return Action(

        type=ActionType.CLICK,

        target=Target(

            name=name,

            role=role,

            css=css,

            exact=exact,

        ),

        reason=reason,

    )



def _type(

    text: str,

    *,

    name: str | None = None,

    role: str | None = None,

    css: str | None = None,

    exact: bool = False,

    reason: str = "Enter the requested text.",

) -> Action:

    return Action(

        type=ActionType.TYPE,

        target=Target(

            name=name,

            role=role,

            css=css,

            exact=exact,

        ),

        text=text,

        reason=reason,

    )



def _press(

    key: str,

    reason: str = "Press the requested key.",

) -> Action:

    return Action(

        type=ActionType.PRESS,

        key=key,

        reason=reason,

    )



def _fast_plan(actions: list[Action]) -> ActionPlan:

    return ActionPlan(

        actions=actions,

        source="fast_path",

    )



# ============================================================

# DETERMINISTIC MULTI-STEP WORKFLOWS

# ============================================================

def _try_deterministic_multi_step(instruction: str) -> ActionPlan | None:
    """No exact-task recipes: complex tasks must be solved generically."""
    return None




# ============================================================

# AMAZON SPECIAL HANDLING

# ============================================================

def _is_amazon_target(text: str) -> bool:

    normalized = _normalise(text)

    return bool(

        re.search(

            r"\bamazon(?:\s+india)?\b",

            normalized,

            flags=re.IGNORECASE,

        )

    )



def _amazon_login_plan() -> ActionPlan:

    """

    Amazon login is intentionally handled as:

        open Amazon homepage

        -> click the Amazon Account & Lists element

    We do not directly use /ap/signin because the user previously

    encountered a non-working direct-login navigation.

    The Amazon account element is selected using its stable CSS

    selector instead of requiring an accessible name/role match.

    This avoids the Amazon header text changing between:

        "Sign in"

        "Hello, sign in"

        "Account & Lists"

    """

    return _fast_plan(

        [

            _navigate(

                "https://www.amazon.in/",

                "Open Amazon India.",

            ),

            _click(

                css="#nav-link-accountList",

                reason="Open the Amazon sign-in page.",

            ),

        ]

    )



def _amazon_cart_plan() -> ActionPlan:

    return _fast_plan(

        [

            _navigate(

                "https://www.amazon.in/",

                "Open Amazon India.",

            ),

            _click(

                name="Cart",

                role="link",

                css="#nav-cart",

                exact=False,

                reason="Open the Amazon shopping cart.",

            ),

        ]

    )



def _amazon_orders_plan() -> ActionPlan:

    return _fast_plan(

        [

            _navigate(

                "https://www.amazon.in/",

                "Open Amazon India.",

            ),

            _click(

                name="Returns & Orders",

                role="link",

                css="#nav-orders",

                exact=False,

                reason="Open Amazon orders.",

            ),

        ]

    )



def _amazon_account_plan() -> ActionPlan:

    return _fast_plan(

        [

            _navigate(

                "https://www.amazon.in/",

                "Open Amazon India.",

            ),

            _click(

                name="Account & Lists",

                role="link",

                css="#nav-link-accountList",

                exact=False,

                reason="Open the Amazon account page.",

            ),

        ]

    )



def _amazon_search_plan(query: str) -> ActionPlan:

    return _fast_plan(

        [

            _navigate(

                "https://www.amazon.in/s?k="

                + quote_plus(query),

                "Open Amazon and search for the requested item.",

            )

        ]

    )



def _try_amazon_special_task(instruction: str) -> ActionPlan | None:

    """

    IMPORTANT:

    Amazon-specific handling MUST happen before generic unknown-site

    detection.

    Otherwise:

        Open Amazon login page

    could incorrectly become:

        https://amazonloginpage.com/

    """

    normalized = _normalise(instruction)

    if not _is_amazon_target(normalized):

        return None

    # Login / sign in.

    if re.search(

        r"\b(login|log in|sign in|signin)\b",

        normalized,

        flags=re.IGNORECASE,

    ):

        return _amazon_login_plan()

    # Orders.

    if re.search(

        r"\b(order|orders|my orders|order history)\b",

        normalized,

        flags=re.IGNORECASE,

    ):

        return _amazon_orders_plan()

    # Cart.

    if re.search(

        r"\b(cart|shopping cart)\b",

        normalized,

        flags=re.IGNORECASE,

    ):

        return _amazon_cart_plan()

    # Account.

    if re.search(

        r"\b(account|account page|account settings)\b",

        normalized,

        flags=re.IGNORECASE,

    ):

        return _amazon_account_plan()

    # Amazon search.

    search_match = re.search(

        r"""

        \bamazon(?:\s+india)?\b

        .\*?

        \b(?:and\s+)?(?:search|look\s+for|find)

        \s+

        (?P<query>.+?)

        $

        """,

        instruction,

        flags=re.IGNORECASE | re.VERBOSE,

    )

    if search_match:

        query = _clean_query(search_match.group("query"))

        if query:

            return _amazon_search_plan(query)

    # Plain Amazon.

    if re.search(

        r"^\s*(?:open|go\s+to|visit|start|launch)\s+"

        r"amazon(?:\s+india)?"

        r"(?:\s+(?:homepage|home|website|site|page))?"

        r"\s*[.!?]?\s*$",

        instruction,

        flags=re.IGNORECASE,

    ):

        return _fast_plan(

            [

                _navigate(

                    "https://www.amazon.in/",

                    "Open Amazon India.",

                )

            ]

        )

    return None



# ============================================================

# ORACLE SPECIAL HANDLING

# ============================================================

def _is_oracle_target(text: str) -> bool:

    normalized = _normalise(text)

    return bool(

        re.search(

            r"\boracle\b",

            normalized,

            flags=re.IGNORECASE,

        )

    )



def _oracle_home_plan() -> ActionPlan:

    return _fast_plan(

        [

            _navigate(

                "https://www.oracle.com/in/",

                "Open Oracle India.",

            )

        ]

    )



def _oracle_login_plan() -> ActionPlan:

    return _fast_plan(

        [

            _navigate(

                "https://www.oracle.com/in/",

                "Open Oracle India.",

            ),

            _click(

                name="Sign In",

                role="link",

                exact=True,

                reason="Open the Oracle sign-in page.",

            ),

        ]

    )



def _oracle_cloud_plan() -> ActionPlan:

    return _fast_plan(

        [

            _navigate(

                "https://cloud.oracle.com/",

                "Open Oracle Cloud.",

            )

        ]

    )



def _try_oracle_special_task(instruction: str) -> ActionPlan | None:

    normalized = _normalise(instruction)

    if not _is_oracle_target(normalized):

        return None

    # Oracle Cloud.

    if re.search(

        r"\boracle\s+cloud\b|\boci\b",

        normalized,

        flags=re.IGNORECASE,

    ):

        return _oracle_cloud_plan()

    # Oracle sign-in/login.

    if re.search(

        r"\b(login|log in|sign in|signin)\b",

        normalized,

        flags=re.IGNORECASE,

    ):

        return _oracle_login_plan()

    # Plain Oracle.

    if re.search(

        r"^\s*(?:open|go\s+to|visit|start|launch)\s+"

        r"oracle"

        r"(?:\s+(?:homepage|home|website|site|page))?"

        r"\s*[.!?]?\s*$",

        instruction,

        flags=re.IGNORECASE,

    ):

        return _oracle_home_plan()

    return None



# ============================================================

# FLIPKART SPECIAL HANDLING

# ============================================================

def _try_flipkart_special_task(instruction: str) -> ActionPlan | None:

    normalized = _normalise(instruction)

    if not re.search(r"\bflipkart\b", normalized):

        return None

    if re.search(

        r"\b(login|log in|sign in|signin)\b",

        normalized,

    ):

        return _fast_plan(

            [

                _navigate(

                    "https://www.flipkart.com/",

                    "Open Flipkart.",

                ),

                _click(

                    name="Login",

                    role="button",

                    exact=True,

                    reason="Open the Flipkart login dialog.",

                ),

            ]

        )

    search_match = re.search(

        r"""

        \bflipkart\b

        .\*?

        \b(?:and\s+)?(?:search|look\s+for|find)

        \s+

        (?P<query>.+?)

        $

        """,

        instruction,

        flags=re.IGNORECASE | re.VERBOSE,

    )

    if search_match:

        query = _clean_query(search_match.group("query"))

        if query:

            return _fast_plan(

                [

                    _navigate(

                        "https://www.flipkart.com/search?q="

                        + quote_plus(query),

                        "Open Flipkart and search for the requested item.",

                    )

                ]

            )

    if re.search(

        r"^\s*(?:open|go\s+to|visit|start|launch)\s+"

        r"flipkart"

        r"(?:\s+(?:homepage|home|website|site|page))?"

        r"\s*[.!?]?\s*$",

        instruction,

        flags=re.IGNORECASE,

    ):

        return _fast_plan(

            [

                _navigate(

                    "https://www.flipkart.com/",

                    "Open Flipkart.",

                )

            ]

        )

    return None



# ============================================================

# GENERIC KNOWN-SITE SEARCH

# ============================================================

def _try_open_and_search(instruction: str) -> ActionPlan | None:

    match = _OPEN_AND_SEARCH_RE.match(instruction)

    if not match:

        return None

    site_text = match.group("site").strip()

    query = _clean_query(match.group("query"))

    if not query:

        return None

    adapter = _site_name_from_text(site_text)

    if adapter is None:

        return None

    if adapter.search_url_template:

        url = adapter.search_url_template.format(

            query=quote_plus(query)

        )

        return _fast_plan(

            [

                _navigate(

                    url,

                    "Open the requested website and search for the requested query.",

                )

            ]

        )

    # Site is known but does not expose a safe predefined search URL.

    # Do NOT silently downgrade a multi-step "find/search" request to

    # opening only the homepage. Returning None sends the full task to

    # Gemini so it can inspect the site and complete the requested action.

    #

    # This preserves simple "Open GitHub" as fast path while routing

    # "Open GitHub and find ..." to the AI planner.

    #

    #

    return None



# ============================================================

# SEARCH "QUERY ON SITE"

# ============================================================

def _try_search_on_site(instruction: str) -> ActionPlan | None:

    match = _SEARCH_ON_SITE_RE.match(instruction)

    if not match:

        return None

    query = _clean_query(match.group("query"))

    site_text = match.group("site").strip()

    if not query:

        return None

    adapter = _site_name_from_text(site_text)

    if adapter is None:

        return None

    if adapter.search_url_template:

        return _fast_plan(

            [

                _navigate(

                    adapter.search_url_template.format(

                        query=quote_plus(query)

                    ),

                    "Search the requested website.",

                )

            ]

        )

    return _fast_plan(

        [

            _navigate(

                adapter.url,

                "Open the requested website.",

            )

        ]

    )



# ============================================================

# KNOWN WEBSITE PAGE HANDLING

# ============================================================

def _known_page_url(

    site: SiteAdapter,

    page_name: str,

) -> str | None:

    normalized_page = _normalise(page_name)

    base = site.url.rstrip("/")

    # GitHub

    if "github" in site.aliases:

        pages = {

            "repositories": "/repositories",

            "repos": "/repositories",

            "issues": "/issues",

            "pull requests": "/pulls",

            "pulls": "/pulls",

            "explore": "/explore",

            "notifications": "/notifications",

            "settings": "/settings/profile",

        }

        if normalized_page in pages:

            return base + pages[normalized_page]

    # YouTube

    if "youtube" in site.aliases:

        pages = {

            "subscriptions": "/feed/subscriptions",

            "history": "/feed/history",

            "trending": "/feed/trending",

        }

        if normalized_page in pages:

            return base + pages[normalized_page]

    # Wikipedia

    if "wikipedia" in site.aliases or "wiki" in site.aliases:

        if normalized_page in {"english", "english wikipedia"}:

            return "https://en.wikipedia.org"

    # LinkedIn

    if "linkedin" in site.aliases or "linked in" in site.aliases:

        pages = {

            "jobs": "/jobs/",

            "my network": "/mynetwork/",

            "network": "/mynetwork/",

            "notifications": "/notifications/",

            "messages": "/messaging/",

        }

        if normalized_page in pages:

            return base + pages[normalized_page]

    return None



def _try_known_site_page(instruction: str) -> ActionPlan | None:

    normalized = _normalise(instruction)

    # Examples:

    # Open GitHub repositories

    # Open YouTube subscriptions

    # Open LinkedIn jobs

    match = re.match(

        r"""

        ^\s*

        (?:open|go\s+to|visit|start|launch)

        \s+

        (?P<site>.+?)

        \s+

        (?P<page>

            repositories|

            repos|

            issues|

            pull\s+requests|

            pulls|

            explore|

            notifications|

            settings|

            subscriptions|

            history|

            trending|

            english\s+wikipedia|

            english|

            jobs|

            my\s+network|

            network|

            messages

        )

        \s*$

        """,

        normalized,

        flags=re.IGNORECASE | re.VERBOSE,

    )

    if not match:

        return None

    site = _site_name_from_text(match.group("site"))

    page = match.group("page")

    if site is None:

        return None

    url = _known_page_url(site, page)

    if url is None:

        return None

    return _fast_plan(

        [

            _navigate(

                url,

                "Open the requested page.",

            )

        ]

    )



# ============================================================

# GLOBAL SEARCH

# ============================================================

def _try_global_search(instruction: str) -> ActionPlan | None:

    normalized = _normalise(instruction)

    # Search ...

    # Search for ...

    # Find ...

    # Look for ...

    match = re.match(

        r"""

        ^\s*

        (?:search|find|look\s+for|google)

        \s+

        (?:for\s+)?

        (?P<query>.+?)

        \s*$

        """,

        instruction,

        flags=re.IGNORECASE | re.VERBOSE,

    )

    if not match:

        return None

    query = _clean_query(match.group("query"))

    if not query:

        return None

    # Avoid treating "search Amazon for laptops" as a global search

    # when a site-specific search can be handled directly.

    site_search = re.match(

        r"""

        ^\s*

        (?P<site>.+?)

        \s+

        (?:for|about)

        \s+

        (?P<query>.+)

        \s*$

        """,

        query,

        flags=re.IGNORECASE | re.VERBOSE,

    )

    if site_search:

        site = _site_name_from_text(site_search.group("site"))

        if site and site.search_url_template:

            real_query = _clean_query(

                site_search.group("query")

            )

            if real_query:

                return _fast_plan(

                    [

                        _navigate(

                            site.search_url_template.format(

                                query=quote_plus(real_query)

                            ),

                            "Search the requested website.",

                        )

                    ]

                )

    return _fast_plan(

        [

            _navigate(

                "https://www.google.com/search?q="

                + quote_plus(query),

                "Search Google for the requested query.",

            )

        ]

    )



# ============================================================

# OPEN KNOWN SITE

# ============================================================

def _try_open_known_site(instruction: str) -> ActionPlan | None:

    if not _OPEN_PREFIX_RE.match(instruction):

        return None

    target_text = _strip_open_prefix(instruction)

    target_text = _clean_query(target_text)

    # Do not treat a multi-step instruction as a simple site open.

    # Those tasks must continue through the remaining fast-path

    # handlers or fall through to Gemini.

    # A site name may legitimately occur inside the user's search query.

    # Example: "Open Google, search Python multithreading tutorial" must

    # resolve to Google, not python.org. Any explicit follow-up verb means

    # this is not a simple site-open request.

    if re.search(

        r"(?:,|\s+(?:and|then)\s+)(?:search|look\s+for|find|click|type|write|press|fill|select|choose|open|scroll|go\s+to)\b",

        target_text,

        flags=re.IGNORECASE,

    ):

        return None

    if not target_text:

        return None

    # Remove common trailing page words only when the entire target

    # is otherwise a known site.

    site = _site_name_from_text(target_text)

    if site is not None:

        return _fast_plan(

            [

                _navigate(

                    site.url,

                    "Open the requested website.",

                )

            ]

        )

    return None

# ============================================================

# EXPLICIT URL

# ============================================================

def _try_explicit_url(instruction: str) -> ActionPlan | None:

    url = _extract_url(instruction)

    if not url:

        return None

    return _fast_plan(

        [

            _navigate(

                url,

                "Navigate to the explicitly requested URL.",

            )

        ]

    )



# ============================================================

# UNKNOWN SIMPLE WEBSITE

# ============================================================

def _extract_unknown_site_target(instruction: str) -> str | None:

    """

    Convert simple commands such as:

        Open eCart

        Open e cart

        Open example.io

        Go to example.org.in

        Visit mysite

    into a possible website target.

    This MUST only be used after all known-site handlers have run.

    """

    if not _OPEN_PREFIX_RE.match(instruction):

        return None

    target = _strip_open_prefix(instruction)

    target = _clean_query(target)

    if not target:

        return None

    # Remove harmless trailing words.

    target = re.sub(

        r"\s+(?:website|site|homepage|home)$",

        "",

        target,

        flags=re.IGNORECASE,

    ).strip()

    if not target:

        return None

    # Do not treat commands with additional actions as a simple site.

    if re.search(

        r"\b(?:and|then)\s+"

        r"(?:search|look\s+for|find|click|type|press|open)\b",

        target,

        flags=re.IGNORECASE,

    ):

        return None


    # ROOT-CAUSE FIX: the exclusion list above only knew a few verbs, so "open chatgpt and say hello" became
    # https://chatgptandsayhello.com/.  A destination is a short NAME; anything that reads like a sentence is a
    # task for the goal runner / planner, never a domain.
    if not is_plain_site_name(target):
        return None

    return target



# Words that make a phrase a SENTENCE (an instruction), not the name of a website.
_NOT_A_SITE_WORD = frozenset("""
a an the and or but then so to for with on in at of from by into onto it its this that these those my me i you us we
say says said tell ask send write compose draft type enter fill submit login log sign signin signup register
search find look lookup click press scroll select choose open go goto visit navigate read extract get give show list
play watch listen check create make add post buy order pay download upload save copy paste summarize summarise
help please can could would should want need like some any good best first second third last next previous
hello hi message email mail form page result results video videos article articles
""".split())


_NOUN_ONLY_WORDS = frozenset("hello hi message email mail form page result results video videos article articles".split())
SENTENCE_WORDS = _NOT_A_SITE_WORD - _NOUN_ONLY_WORDS      # verbs / conjunctions / pronouns: evidence of a sentence


def looks_like_sentence(text: str) -> bool:
    """True when the phrase contains verbs, conjunctions or pronouns (so it is an instruction, not a name or a link label)."""
    words = [w for w in re.split(r"[^a-z0-9']+", str(text or "").lower()) if w]
    return any(w in SENTENCE_WORDS for w in words)


def is_plain_site_name(text: str) -> bool:
    """True only for a short bare name such as ``eCart``, ``e cart`` or ``shopzone`` (<= 3 plain words).

    Used before ever turning free text into a ``<name>.com`` guess.  Conjunctions, verbs, pronouns and
    task nouns disqualify the phrase, so a whole instruction can never be glued into a domain.
    """
    value = str(text or "").strip().strip(".,!?;:")
    if not value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]*", value):
        return False
    words = [w for w in value.lower().split() if w]
    if not words or len(words) > 3:
        return False
    return not any(w in _NOT_A_SITE_WORD for w in words)



def _try_unknown_simple_site(instruction: str) -> ActionPlan | None:

    target = _extract_unknown_site_target(instruction)

    if not target:

        return None

    # Explicit URL/domain.

    explicit_url = _extract_url(target)

    if explicit_url:

        return _fast_plan(

            [

                _navigate(

                    explicit_url,

                    "Navigate to the requested website.",

                )

            ]

        )

    domain_url = _domain_to_url(target)

    if domain_url:

        return _fast_plan(

            [

                _navigate(

                    domain_url,

                    "Navigate to the requested website.",

                )

            ]

        )

    # Only create a .com URL for a clear simple open command.

    #

    # Examples:

    #   Open eCart

    #   Open e cart

    #   Open example

    #

    # We deliberately do NOT do this for arbitrary sentences.

    if re.fullmatch(

        r"[a-zA-Z0-9][a-zA-Z0-9 _-]{0,80}",

        target,

    ):

        compact = re.sub(

            r"[^a-zA-Z0-9]",

            "",

            target,

        ).lower()

        if not compact:

            return None

        return _fast_plan(

            [

                _navigate(

                    f"https://{compact}.com/",

                    "Open the requested website.",

                )

            ]

        )

    return None



# ============================================================

# SIMPLE NEW-TAB COMMAND

# ============================================================

def _try_new_tab_command(instruction: str) -> ActionPlan | None:

    normalized = _normalise(instruction)

    if normalized in {

        "new tab",

        "open a new tab",

        "create a new tab",

        "create new tab",

    }:

        # A new tab is a browser-lifecycle operation, not an HTTP navigation.
        # The orchestrator owns creation of the dedicated task tab, so this
        # planner must not manufacture an invalid about:blank navigate action.
        return ActionPlan(
            actions=[],
            source="fast_path",
            done=True,
            goal="Open a new browser tab.",
            expected_state="A fresh live browser tab exists.",
        )

    return None



# ============================================================

# CHROME COMMAND

# ============================================================

def _try_chrome_command(instruction: str) -> ActionPlan | None:

    normalized = _normalise(instruction)

    # Because the application itself launches a dedicated Chrome

    # session, "Open Chrome" means create/navigate the task tab.

    if normalized in {

        "open chrome",

        "go to chrome",

        "visit chrome",

        "start chrome",

        "launch chrome",

    }:

        # Chrome/tab creation is owned by the browser manager, not an HTTP
        # navigate action.
        return ActionPlan(
            actions=[],
            source="fast_path",
            done=True,
            goal="Open Chrome.",
            expected_state="A fresh live browser tab exists.",
        )


    # Open Chrome and search ...

    match = re.match(

        r"""

        ^\s*

        (?:open|start|launch)\s+chrome

        \s+

        (?:and\s+)?

        (?:search|look\s+for|find)

        \s+

        (?P<query>.+?)

        \s*$

        """,

        instruction,

        flags=re.IGNORECASE | re.VERBOSE,

    )

    if match:

        query = _clean_query(match.group("query"))

        if query:

            return _fast_plan(

                [

                    _navigate(

                        "https://www.google.com/search?q="

                        + quote_plus(query),

                        "Open Chrome and search Google for the requested query.",

                    )

                ]

            )

    return None



# ============================================================

# LIVE LOCAL ACTION ROUTER

# ============================================================

def _context_dict(page_context: str | dict | None) -> dict:

    if isinstance(page_context, dict):

        return page_context

    if isinstance(page_context, str):

        try:

            import json

            value = json.loads(page_context)

            return value if isinstance(value, dict) else {}

        except Exception:

            return {}

    return {}



def _interactive_target(item: dict, *, fallback_role: str | None = None) -> Target | None:

    role = str(item.get("role") or fallback_role or "").strip() or None

    name = str(item.get("name") or item.get("aria") or item.get("text") or item.get("title") or item.get("placeholder") or "").strip()

    css = item.get("css")

    placeholder = str(item.get("placeholder") or "").strip() or None

    if not role and not css and not name and not placeholder:

        return None

    return Target(

        role=role,

        name=name or None,

        css=str(css) if css else None,

        placeholder=placeholder,

    )



def _search_target(context: dict) -> Target | None:

    elements = context.get("elements") or context.get("interactive_elements") or []

    candidates: list[tuple[int, dict]] = []

    for item in elements:

        if not isinstance(item, dict) or item.get("disabled"):

            continue

        hay = " ".join(str(item.get(k) or "") for k in ("name", "text", "title", "placeholder", "name_attr", "role", "input_type")).lower()

        tag = str(item.get("tag") or "").lower()

        role = str(item.get("role") or "").lower()

        score = 0

        if role in {"searchbox", "textbox"}: score += 80

        if "search" in hay: score += 70

        if tag in {"input", "textarea"}: score += 20

        if str(item.get("input_type") or "").lower() == "search": score += 35

        if score:

            try:

                y = float(item.get("box", {}).get("y", item.get("y", 9999)))

            except Exception:

                y = 9999

            score -= min(y / 500, 10)

            candidates.append((score, item))

    if not candidates:

        return None

    candidates.sort(key=lambda pair: (-pair[0], int(pair[1].get("index", 0))))

    item = candidates[0][1]

    return _interactive_target(item, fallback_role="textbox")



def _query_from_instruction(instruction: str) -> str | None:
    """Extract the text to type for the FIRST search, with task-control language removed.

    Uses the goal decomposer (agent/goals.py) so "search for A, then go back and search for B"
    yields "A" - never "A, then go back and search for B".
    """
    from agent.goals import clean_search_queries
    from agent.site_resolver import resolve_site_url

    clean = clean_search_queries(instruction, resolve_site_url)
    if clean:
        return clean[0]
    return _query_from_instruction_legacy(instruction)


def _query_from_instruction_legacy(instruction: str) -> str | None:
    """Fallback for phrasings the decomposer does not treat as a search (e.g. "find X")."""
    text = re.sub(r"\s+", " ", instruction.strip())
    patterns = [
        r"\b(?:search|look\s+for|find)\s+(?:for\s+)?(?P<query>.+?)(?=\s*(?:,|;|\band\b|\bthen\b)\s*(?:open|click|go\s+to|visit|tell|give|show|return|find|locate|scroll|download|extract)\b|$)",
        r"\b(?:search|look\s+for|find)\s+(?:for\s+)?(?P<query>.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            query = _clean_query(match.group("query"))
            query = re.sub(r"\s*(?:,|;)\s*$", "", query).strip()
            if query:
                return query
    return None




def _has_completed_type(completed: list[dict], query: str) -> bool:

    for item in completed:

        action = item.get("action") if isinstance(item, dict) else None

        if isinstance(action, dict) and action.get("type") == "type" and str(action.get("text") or "").strip().lower() == query.strip().lower():

            return True

    return False



def _has_completed_press(completed: list[dict]) -> bool:

    for item in completed:

        action = item.get("action") if isinstance(item, dict) else None

        if isinstance(action, dict) and action.get("type") == "press" and str(action.get("key") or "").lower() in {"enter", "return"}:

            return True

    return False



def _ranked_result_elements(context: dict, instruction: str) -> list[dict]:
    """All plausible result elements, best first (same scoring as the first-result picker)."""

    elements = context.get("elements") or context.get("interactive_elements") or []

    url = str(context.get("current_url") or context.get("url") or "").lower()

    wants_video = bool(re.search(r"\bvideo\b", instruction, re.IGNORECASE))

    wants_article = bool(re.search(r"\barticle\b", instruction, re.IGNORECASE))

    wants_repo = bool(re.search(r"\brepository\b|\brepo\b", instruction, re.IGNORECASE))
    query = _query_from_instruction(instruction) or ""
    query_tokens = [
        token for token in re.findall(r"[a-z0-9]{3,}", query.lower())
        if token not in {"the", "and", "for", "first", "result"}
    ]

    candidates: list[tuple[float, dict]] = []

    for item in elements:

        if not isinstance(item, dict) or item.get("disabled"):

            continue

        href = str(item.get("href") or "").lower()

        text = str(item.get("text") or "").strip()

        name = str(item.get("name") or "").strip()

        hay = f"{text} {name} {href}".lower()

        tag = str(item.get("tag") or "").lower()

        if not href and tag != "button":

            continue

        # A search/submit control is not a result.  Buttons are allowed for
        # modern result cards, but generic controls such as Search, Submit,
        # Filter, Sort, Login, and Menu must never be selected as the
        # requested result merely because they are prominent in the DOM.
        if tag == "button" and re.search(
            r"\b(?:search|submit|filter|sort|go|login|sign in|menu|next|previous)\b",
            hay,
            flags=re.IGNORECASE,
        ):
            continue

        score = 0.0

        if wants_video and ("/watch" in href or "/shorts/" in href): score += 120

        if wants_article and "/wiki/" in href and not any(x in href for x in ("special:", "help:", "file:", "category:", "portal:")): score += 110

        if wants_repo and re.match(r"^/[^/]+/[^/?#]+$", href): score += 100

        if not wants_video and not wants_article and not wants_repo:
            score += 25 if tag == "a" else 10

        # Generic relevance scoring: prefer result text that actually
        # contains words from the user's requested query.
        if query_tokens:
            overlap = sum(1 for token in query_tokens if token in hay)
            score += overlap * 28

            # A result whose visible title/name contains the complete query
            # phrase is stronger evidence of relevance than a related page
            # that merely contains the same words. This prevents generic
            # pages such as "Neural network (machine learning)" from beating
            # an exact "Machine learning" result.
            query_phrase = re.sub(r"\s+", " ", query.lower()).strip()
            text_norm = re.sub(r"\s+", " ", f"{text} {name}".lower()).strip()
            if query_phrase and text_norm == query_phrase:
                score += 260
            elif query_phrase and query_phrase in text_norm:
                score += 170
            # Prefer a short, direct title over a longer related page.
            # This is generic result ranking: an exact entity match should
            # beat a timeline/list/journal page that merely contains it.
            title_words = re.findall(r"[a-z0-9]+", text_norm)
            query_words = re.findall(r"[a-z0-9]+", query_phrase)
            # For "open the FIRST <thing>" on a generic result list (courses, products, listings...)
            # the user asked for DOM order, not for the shortest title - so the length penalty
            # is skipped there (video/article/repository requests keep their existing ranking).
            ordinal_first = bool(re.search(r"\b(?:first|top|1st)\b", instruction, re.IGNORECASE))
            generic_kind = not (wants_video or wants_article or wants_repo)
            if query_words and title_words and not (ordinal_first and generic_kind):
                extra_words = max(0, len(title_words) - len(query_words))
                score -= min(70, extra_words * 10)

        region = str(item.get("region") or "").lower()
        if region in {"main", "article", "content"}:
            score += 18
        elif region in {"header", "nav", "sidebar", "footer"}:
            score -= 30

        if any(word in hay for word in ("sign in", "login", "menu", "settings", "privacy", "terms", "subscribe", "advertisement")):
            score -= 80

        # When the task asks for an article/result, avoid common navigation
        # and publication metadata links unless their text is a much stronger
        # match. These are generic page semantics, not website recipes.
        if wants_article and any(word in hay for word in (
            "journal", "magazine", "category", "disambiguation", "talk",
            "edit", "help", "special:", "portal:", "timeline", "list of",
            "history of", "archive", "index"
        )):
            score -= 95

        try:

            y = float(item.get("box", {}).get("y", item.get("y", 9999)))

        except Exception:

            y = 9999

        score += max(0, 30 - min(y / 50, 30))

        if text or name: score += 8

        if score > 0:

            candidates.append((score, item))

    candidates.sort(key=lambda pair: (-pair[0], int(pair[1].get("index", 0))))

    return [item for _, item in candidates]


def _first_result_target(context: dict, instruction: str) -> Target | None:
    ranked = _ranked_result_elements(context, instruction)
    if not ranked:
        return None
    return _interactive_target(ranked[0], fallback_role="link")


def _search_has_post_search_state_change(completed: list[dict], query: str) -> bool:
    """Return True once the browser has already left the search-submission stage.

    A live-page optimization must never keep clicking arbitrary links from an
    article/detail page just because the original task contains the words
    "first result". Once the requested search has been typed and submitted,
    any successful click/navigation after that submission means result
    selection has already been attempted. Further recovery belongs to the
    adaptive planner, which can inspect the new page and choose a different
    strategy.
    """
    query_norm = str(query or '').strip().lower()
    submitted = False
    for item in completed:
        action = item.get('action') if isinstance(item, dict) else None
        if not isinstance(action, dict):
            continue
        typ = str(action.get('type') or '').lower()
        if typ == 'type' and str(action.get('text') or '').strip().lower() == query_norm:
            continue
        if typ == 'press' and str(action.get('key') or '').lower() in {'enter', 'return'}:
            submitted = True
            continue
        if not submitted:
            continue
        if typ in {'click', 'navigate'}:
            return True
    return False


def _looks_like_search_results_page(context: dict, query: str) -> bool:
    """Conservatively identify a live search/results page from generic evidence."""
    url = str(context.get('current_url') or context.get('url') or '').lower()
    title = str(context.get('current_title') or context.get('title') or '').lower()
    body = str(context.get('body_text') or '').lower()
    if any(marker in url for marker in ('/search', '?q=', '&q=', '?query=', '&query=', 'search_query=')):
        return True
    if 'search results' in title or 'search results' in body[:5000]:
        return True
    tokens = [t for t in re.findall(r'[a-z0-9]{3,}', str(query).lower()) if t not in {'the','and','for','first','result'}]
    if tokens and all(t in body for t in tokens) and re.search(r'\b(?:results|found|matching|search)\b', body):
        return True
    return False


def _detail_page_matches_query(context: dict, query: str) -> bool:
    """Conservatively recognize a detail page whose title (brand suffix removed) or
    first heading is the searched entity."""
    from agent.page_identity import page_is_entity

    return page_is_entity(context, query)


def _extraction_reason_from_instruction(instruction: str) -> str:
    """Turn the user's information request into a compact semantic-extraction reason."""
    text = re.sub(r"\s+", " ", str(instruction or "")).strip()
    match = re.search(
        r"\bsection\b(?:\s+that\s+(?:explains?|describes?|covers?)|\s+(?:about|on|for|concerning))\s+(.+?)(?=\s*,?\s+and\s+(?:tell|give|show|provide|return|report|read)\b|$)",
        text,
        flags=re.I,
    )
    if match:
        topic = match.group(1).strip(" ,.;")
        if re.search(r"\bfirst\s+sentence\b", text, re.I):
            return f"Extract the first sentence from the section about {topic}."
        return f"Extract the relevant section about {topic}."
    if re.search(r"\bfirst\s+sentence\b", text, re.I):
        return "Extract the first sentence from the relevant section on the current page."
    if re.search(r"\b(?:page\s+)?title\b", text, re.I):
        return "Extract the title of the current page."
    if re.search(r"\bfirst\s+heading\b", text, re.I):
        return "Extract the first heading from the current page."
    if re.search(r"\b(?:heading|headings)\b", text, re.I):
        return "Extract the relevant heading from the current page."
    return "Extract the requested information from the relevant content on the current page."


def try_live_path(

    instruction: str,

    page_context: str | dict | None,

) -> ActionPlan | None:

    """Choose obvious live-page actions without spending an LLM call.

    This is intentionally conservative: it only handles search -> Enter ->

    first result/video/article/repository patterns when the live DOM provides

    a matching control/result. Everything ambiguous is left to Ollama.

    """

    context = _context_dict(page_context)

    if not context:

        return None

    completed = context.get("completed_actions") or []

    url = str(context.get("current_url") or context.get("url") or "").lower()

    query = _query_from_instruction(instruction)

    if not query:

        return None

    wants_first = bool(re.search(r"\b(?:first|top)\b", instruction, re.IGNORECASE))

    wants_result = bool(re.search(r"\b(?:result|video|article|repository|repo|link)\b", instruction, re.IGNORECASE)) or bool(
        # generic: "open the first <anything>" (course, product, page, listing, ...)
        re.search(r"\b(?:open|click|select|choose|go\s+to)\s+(?:on\s+)?(?:the\s+)?(?:first|top|1st)\s+\w+", instruction, re.IGNORECASE)
    )

    # If the requested search has not happened yet, type into the live search

    # control. This works across sites because it uses the inspected DOM.

    if not _has_completed_type(completed, query):

        target = _search_target(context)

        if target is not None:

            return ActionPlan(

                actions=[Action(type=ActionType.TYPE, target=target, text=query, reason=f"Search for {query}." )],

                source="fast_path",

                done=False,

            )

    # After typing, press Enter once. This avoids asking a local model to

    # rediscover the standard search submission action.

    if _has_completed_type(completed, query) and not _has_completed_press(completed):
        target = _search_target(context)
        return ActionPlan(
            actions=[Action(
                type=ActionType.PRESS,
                key="Enter",
                target=target,
                reason="Submit the search in the active search control.",
            )],
            source="fast_path",
            done=False,
        )

    # A live fast-path must never declare a multi-stage task complete merely
    # because the URL happens to look like an article/video/repository page.
    # The browser agent needs verified action history (search -> submit ->
    # result selection -> requested extraction) before completion.
    #
    # Search-only tasks are the one safe exception: after a real search submit,
    # they can finish only when the user did not request any later result or
    # information.
    asks_for_answer = bool(re.search(
        r"\b(?:tell|give|show|return|extract|read|report)\b",
        instruction,
        re.IGNORECASE,
    ))
    if (
        _has_completed_press(completed)
        and not wants_first
        and not wants_result
        and not asks_for_answer
    ):
        return ActionPlan(actions=[], source="fast_path", done=True)

    # Do not perform title/sentence/content extraction in the fast path.
    # The adaptive planner must choose the actual live DOM target so that
    # arbitrary websites and non-standard layouts are handled correctly.

    if wants_first and wants_result:
        # This optimization is ONLY allowed while we are demonstrably on a
        # search-results state. Previously it kept running after the first
        # result had already been opened, which caused NEXA to click unrelated
        # links such as a journal link inside the article. Once any successful
        # click/navigation has occurred after search submission, hand control
        # back to the adaptive planner.
        if not _search_has_post_search_state_change(completed, query) and _looks_like_search_results_page(context, query):
            target = _first_result_target(context, instruction)

            if target is not None:
                return ActionPlan(
                    actions=[Action(
                        type=ActionType.CLICK,
                        target=target,
                        reason="Open the first relevant result supported by the current live search-results DOM.",
                    )],
                    source="fast_path",
                    done=False,
                )

        # If the requested result is already open and the user asked for
        # information from it, the generic DOM fallback can perform the
        # extraction without another model call. This is deliberately based
        # on title/query evidence, never on a named website or URL recipe.
        if asks_for_answer and _detail_page_matches_query(context, query):
            reason = _extraction_reason_from_instruction(instruction)
            return ActionPlan(
                actions=[Action(
                    type=ActionType.EXTRACT,
                    target=None,
                    reason=reason,
                )],
                source="fast_path",
                done=False,
            )

    return None



# ============================================================

# MAIN FAST ROUTER

# ============================================================

def try_fast_path(instruction: str) -> ActionPlan | None:
    """Generic browser bootstrap only; never hard-code a complete task."""
    if not isinstance(instruction, str):
        return None
    original = instruction.strip()
    if not original:
        return None

    for handler in (_try_chrome_command, _try_new_tab_command, _try_explicit_url, _try_unknown_simple_site):
        plan = handler(original)
        if plan is not None:
            return plan

    # Named-site bootstrap is generic site knowledge, not a workflow.
    # Every subsequent action is chosen from the live DOM.
    return _try_initial_known_site_navigation(original)




# ============================================================

# PUBLIC COMPATIBILITY HELPERS

# ============================================================

def build_fast_path(instruction: str) -> ActionPlan | None:

    """

    Compatibility wrapper for older code that imports

    build_fast_path().

    """

    return try_fast_path(instruction)



def plan_simple_task(instruction: str) -> ActionPlan | None:

    """

    Compatibility wrapper for older code that imports

    plan_simple_task().

    """

    return try_fast_path(instruction)



# ============================================================

# DEBUG / DEVELOPMENT HELPERS

# ============================================================

def describe_fast_path(instruction: str) -> str:

    """

    Return a small human-readable description useful for debugging.

    This does not execute the browser.

    """

    plan = try_fast_path(instruction)

    if plan is None:

        return "AI_REQUIRED"

    if not plan.actions:

        return "EMPTY_PLAN"

    descriptions: list[str] = []

    for action in plan.actions:

        if action.type == ActionType.NAVIGATE:

            descriptions.append(

                f"NAVIGATE {action.url}"

            )

        elif action.type == ActionType.CLICK:

            target = action.target

            if target is None:

                descriptions.append("CLICK")

            else:

                descriptions.append(

                    "CLICK "

                    f"name={target.name!r} "

                    f"role={target.role!r} "

                    f"css={target.css!r}"

                )

        elif action.type == ActionType.TYPE:

            descriptions.append(

                f"TYPE {action.text!r}"

            )

        elif action.type == ActionType.PRESS:

            descriptions.append(

                f"PRESS {action.key!r}"

            )

        else:

            descriptions.append(

                action.type.value.upper()

            )

    return " -> ".join(descriptions)



# ============================================================

# COMMON SIMPLE-TASK TEST CASES

# ============================================================

PLANNER_TEST_CASES = [
    "Open example.com",
    "Open https://example.com/docs",
    "Open an unfamiliar website and search for machine learning",
    "Open a website, search for a topic, open the first relevant result",
    "Search for Python interview questions",
    "Find a tutorial and read its title",
    "Open a new tab",
    "Open Chrome",
]


def run_planner_self_test() -> list[tuple[str, str]]:

    """

    Development helper.

    Returns:

        [(instruction, result_description), ...]

    """

    results: list[tuple[str, str]] = []

    for instruction in PLANNER_TEST_CASES:

        results.append(

            (

                instruction,

                describe_fast_path(instruction),

            )

        )

    return results