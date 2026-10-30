"""Conversational paraphrase dataset for the language layer (no browser, no AI).

Each row: instruction -> expected ordered goal kinds, expected destination (site/url fragment of the first navigation, or None),
and key parameters that must appear.  ``supported=False`` rows are KNOWN GAPS: they document what the deterministic layer cannot
yet read (they are expected to be handed to the AI planner / a clarification), and are reported separately, never hidden.
"""
N, S, T, P, C, X, E, B = "navigate", "search", "type", "press", "click", "extract", "compose", "adaptive"

ROWS = [
    # --- ChatGPT: say / tell / ask -------------------------------------------------------------
    ("open chatgpt and say hello", [N, T, P], "chatgpt.com", {"text": "hello"}, True),
    ("Open ChatGPT and say hello.", [N, T, P], "chatgpt.com", {"text": "hello"}, True),
    ("Can you go to ChatGPT and tell it hello for me?", [N, T, P], "chatgpt.com", {"text": "hello"}, True),
    ("could you please open chat gpt and say hello", [N, T, P], "chatgpt.com", {"text": "hello"}, True),
    ("hey nexa open chatgpt and ask it what is python", [N, T, P], "chatgpt.com", {"text": "what is python"}, True),
    ("go to chatgpt then say hi there", [N, T, P], "chatgpt.com", {"text": "hi there"}, True),
    ("open chatgpt and type hello", [N, T], "chatgpt.com", {"text": "hello"}, True),
    ("open chatgpt", [N], "chatgpt.com", {}, True),
    # --- YouTube ---------------------------------------------------------------------------------
    ("Open YouTube and search Python.", [N, S], "youtube.com", {"query": "Python"}, True),
    ("open youtube and search python tutorials", [N, S], "youtube.com", {"query": "python tutorials"}, True),
    ("Hey, open YouTube and search for Python tutorials please", [N, S], "youtube.com", {"query": "Python tutorials"}, True),
    ("Go on YouTube, search Python tutorials, and open the first video", [N, S, C], "youtube.com", {"query": "Python tutorials"}, True),
    ("open youtube search python tutorials open the first video", [N, S, C], "youtube.com", {"query": "python tutorials"}, True),
    ("Can you open YouTube and search for Python tutorials and open a good result?", [N, S, C], "youtube.com", {"query": "Python tutorials"}, True),
    # --- Wikipedia / GitHub / search ----------------------------------------------------------
    ("Open Wikipedia and search artificial intelligence", [N, S], "wikipedia.org", {"query": "artificial intelligence"}, True),
    ("open wikipedia search databases open the first article and tell me the first heading", [N, S, C, X], "wikipedia.org", {"query": "databases"}, True),
    ("Open Wikipedia, search for databases, open the most relevant article, and report the first heading", [N, S, C, X], "wikipedia.org", {"query": "databases"}, True),
    ("Open GitHub and search for browser automation repositories", [N, S], "github.com", {"query": "browser automation repositories"}, True),
    ("Search for Python interview questions", [S], None, {"query": "Python interview questions"}, True),
    ("Could you look up Python interview questions and open a good result?", [S, C], None, {"query": "Python interview questions"}, True),
    ("Open Google, search for Python tutorials, then go back and open the second result", [N, S, "back", C], "google.com", {"query": "Python tutorials"}, True),
    # --- e-mail drafts ---------------------------------------------------------------------------
    ("Open Gmail and compose an email to rahul@example.com saying hello", [N, E], "mail.google.com", {"recipient": "rahul@example.com", "body": "hello"}, True),
    ("open gmail and write a mail to rahul@example.com saying hello", [N, E], "mail.google.com", {"recipient": "rahul@example.com", "body": "hello"}, True),
    ("Open my Gmail and write an email to this person saying hello", [N, E], "mail.google.com", {"body": "hello"}, True),
    # --- logins: only the sign-in page is opened (never credentials) -------------------------
    ("open amazon and fill in the login form", [N, C], "amazon", {}, True),
    ("open flipkart and fill the login form", [N, C], "flipkart", {}, True),
    ("I need to log in to Amazon. Open the login page and help me fill it in.", [N, C], "amazon", {}, True),
    # --- known gaps (handed to the AI planner / clarification, documented not hidden) ----------------
    ("I want to learn Python. Find a good tutorial video.", [B], None, {}, False),
    ("Find me a Python tutorial and play it.", [B], None, {}, False),
]
