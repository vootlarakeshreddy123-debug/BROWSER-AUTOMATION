"""LOCAL FIXTURE websites for integration testing (NOT real websites).

Two differently-structured dynamic sites served from localhost:
  * "learn": course catalog - header search + decoy footer search box, results
    that render ~350ms after load, pricing plans that render after a delay.
  * "docs": documentation - links hidden behind a Menu button, duplicate
    "Guides" links (menu + footer), a sponsored trap link before real content.
Plus /hang (never answers in time) to exercise navigation timeouts.
"""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CSS = "<style>body{font-family:sans-serif;margin:0}header,footer,main{padding:12px 24px}nav a{margin-right:14px}</style>"


def page(title: str, body: str, script: str = "") -> bytes:
    return (f"<!doctype html><html><head><meta charset=utf-8><title>{title}</title>{CSS}</head>"
            f"<body>{body}<script>{script}</script></body></html>").encode()


LEARN_NAV = ('<header><nav aria-label="Main"><a href="/">Home</a><a href="/explore">Explore</a>'
             '<a href="/pricing">Pricing</a><a href="/login">Sign in</a></nav></header>')
LEARN_FOOT = ('<footer><form onsubmit="return false"><input type="search" placeholder="Search help articles" '
              'aria-label="Search help"></form><p>LearnHub 2026</p></footer>')


def learn(path: str, q: dict) -> bytes | None:
    if path == "/":
        return page("LearnHub - Learn anything", LEARN_NAV + '''<main><h1>Learn anything</h1>
<form role="search" id="f"><input type="search" placeholder="What do you want to learn?" aria-label="Search courses" id="q">
<button type="submit">Search</button></form></main>''' + LEARN_FOOT,
            "document.getElementById('f').addEventListener('submit',e=>{e.preventDefault();"
            "location.href='/search?query='+encodeURIComponent(document.getElementById('q').value)})")
    if path == "/search":
        term = (q.get("query") or [""])[0]
        return page(f"Results for {term} - LearnHub", LEARN_NAV + f'''<main><h1>Results for {term}</h1>
<div id="res"><p>Loading results...</p></div></main>''' + LEARN_FOOT,
            "setTimeout(()=>{const t=%r;document.getElementById('res').innerHTML="
            "[1,2,3,4].map(n=>`<article><h3><a href=\"/course/${n}\">${t} course ${n}: Fundamentals</a></h3>"
            "<p>A course about ${t}.</p></article>`).join('')},350)" % term)
    if path.startswith("/course/"):
        n = path.rsplit("/", 1)[1]
        return page(f"Course {n} - LearnHub", LEARN_NAV + f'''<main><h1>Course {n}: Fundamentals</h1>
<h2>Syllabus</h2><p>Week 1 introduces the basics.</p><h2>Prerequisites</h2><p>None required.</p></main>''')
    if path == "/pricing":
        return page("Pricing - LearnHub", LEARN_NAV + '<main><h1>Pricing</h1><div id="plans"><p>Loading plans...</p></div></main>' + LEARN_FOOT,
            "setTimeout(()=>{document.getElementById('plans').innerHTML=['Solo','Team','Enterprise'].map(p=>"
            "`<section class=plan><h3>${p} plan</h3><a href=\"/pricing/${p.toLowerCase()}\">View ${p} plan details</a></section>`).join('')},300)")
    if path.startswith("/pricing/"):
        p = path.rsplit("/", 1)[1].capitalize()
        return page(f"{p} plan - LearnHub", LEARN_NAV + f'<main><h1>{p} plan</h1><h2>What is included</h2><ul><li>Unlimited courses</li></ul></main>')
    if path in ("/explore", "/login"):
        return page(f"{path[1:].title()} - LearnHub", LEARN_NAV + f"<main><h1>{path[1:].title()}</h1></main>")
    return None


DOCS_FOOT = '<footer><a href="/guides">Guides</a> <a href="/changelog">Changelog</a></footer>'


def docs(path: str, q: dict) -> bytes | None:
    if path == "/":
        return page("DocsWorld", '''<header><a href="/ad">Sponsored: Win a prize today</a>
<button id="m" aria-expanded="false">Menu</button>
<div id="menu" hidden><a href="/guides">Guides</a> <a href="/api">API reference</a> <a href="/changelog">Changelog</a></div></header>
<main><h1>DocsWorld</h1><p>Documentation for everything.</p></main>''' + DOCS_FOOT,
            "document.getElementById('m').onclick=()=>{const m=document.getElementById('menu');m.hidden=!m.hidden;"
            "document.getElementById('m').setAttribute('aria-expanded',String(!m.hidden))}")
    if path == "/guides":
        return page("Guides - DocsWorld", '<header><a href="/">DocsWorld</a></header><main><h1>Guides</h1>'
                    '<ul><li><a href="/guides/getting-started">Getting Started</a></li>'
                    '<li><a href="/guides/advanced">Advanced usage</a></li></ul></main>' + DOCS_FOOT)
    if path == "/guides/getting-started":
        return page("Getting Started - DocsWorld", '<header><a href="/">DocsWorld</a></header><main><h1>Getting Started</h1>'
                    '<h2>Installation</h2><p>Install the package.</p></main>' + DOCS_FOOT)
    if path == "/ad":
        return page("Sponsored prize", "<main><h1>You won a sponsored prize</h1></main>")
    if path in ("/api", "/changelog", "/guides/advanced"):
        return page(path[1:].title() + " - DocsWorld", f"<main><h1>{path[1:].title()}</h1></main>" + DOCS_FOOT)
    return None


def make_handler(site):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path == "/hang":
                time.sleep(40)
                return
            body = site(u.path, parse_qs(u.query))
            if isinstance(body, tuple) and body and body[0] == "REDIRECT":
                self.send_response(302); self.send_header("Location", body[1]); self.end_headers(); return
            if body is None:
                self.send_response(404); self.end_headers(); self.wfile.write(b"not found"); return
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass
    return H


class Sites:
    def __init__(self) -> None:
        self.servers = []
        self.urls = {}
        from tests.fixture_site_extra import blocked, chat, forms, mail, courses, gsearch, hub, longpage, shop, vsite, wiki, gmaillike
        for name, fn in (("learn", learn), ("docs", docs), ("wiki", wiki), ("courses", courses), ("chat", chat), ("hub", hub), ("gsearch", gsearch), ("vsite", vsite), ("longpage", longpage), ("blocked", blocked), ("forms", forms), ("mail", mail), ("shop", shop), ("gmaillike", gmaillike)):
            srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(fn))
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.servers.append(srv)
            self.urls[name] = f"http://127.0.0.1:{srv.server_address[1]}"

    def close(self) -> None:
        for s in self.servers:
            s.shutdown()
