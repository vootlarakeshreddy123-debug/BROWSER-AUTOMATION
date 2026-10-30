"""More LOCAL FIXTURE sites that imitate BEHAVIOURS seen on real sites (not the sites themselves):

  wiki    - search submit redirects straight to the article; heading + paragraph text
  courses - result links open in a NEW TAB (target=_blank); ?slow=1 opens it 1.5 s after the click
  chat    - message composer variants (contenteditable role=textbox / textarea / bare / login wall);
            window.__sent counts any "send" so tests can prove Enter was NOT pressed
  hub     - a link that opens a partner page in a new tab, which has its own links
"""
from __future__ import annotations

from tests.fixture_site import page

WIKI_HEAD = (
    '<header><a href="/">WikiLike</a> <form action="/w/search" role="search"><input type="search" '
    'name="search" placeholder="Search WikiLike" aria-label="Search WikiLike"><button type="submit">Search</button></form>'
    '<nav><a href="/w/Main_Page">Main page</a> <a href="/w/Special:Random">Random article</a></nav></header>'
)


def wiki(path, q):
    if path == "/":
        return page("WikiLike", WIKI_HEAD + "<main><h1>Welcome to WikiLike</h1><p>Search the encyclopedia.</p></main>")
    if path == "/w/search":
        term = (q.get("search") or [""])[0].strip().lower()
        if term == "artificial intelligence":
            return ("REDIRECT", "/wiki/Artificial_intelligence")
        return page("Search results", WIKI_HEAD + f'<main><h1>Search results for {term}</h1><ul><li><a href="/wiki/Artificial_intelligence">Artificial intelligence</a></li></ul></main>')
    if path == "/wiki/Artificial_intelligence":
        body = (
            WIKI_HEAD
            + '<main id="content"><h1 id="firstHeading">Artificial intelligence</h1>'
            + "<p>Artificial intelligence (AI) is the capability of computational systems to perform tasks associated with human intelligence.</p>"
            + "<h2>Goals</h2><p>AI developers argue that this is the only way to deliver valuable applications of the technology.</p>"
            + "<h2>Applications</h2><p>Applications include search engines and recommendation systems.</p>"
            + "<h3>Health</h3><p>Used in imaging.</p></main>"
            + '<footer><a href="/w/About">About</a></footer>'
        )
        return page("Artificial intelligence - WikiLike", body)
    return None


COURSE_NAV = '<header><a href="/">CourseHub</a> <nav><a href="/explore">Explore</a> <a href="/login">Log in</a></nav></header>'
COURSES = (
    ("machine-learning-with-python", "Machine Learning with Python"),
    ("machine-learning-foundations", "Machine Learning Foundations"),
    ("ml-for-everyone", "ML for Everyone"),
)


def courses(path, q):
    if path == "/":
        return page(
            "CourseHub",
            COURSE_NAV + '<main><h1>Learn on CourseHub</h1><form id="f" role="search"><input type="search" '
            'placeholder="What do you want to learn?" aria-label="Search courses" id="q"><button type="submit">Search</button></form></main>',
            "document.getElementById('f').addEventListener('submit',function(e){e.preventDefault();"
            "location.href='/search?query='+encodeURIComponent(document.getElementById('q').value)})",
        )
    if path == "/search":
        term = (q.get("query") or [""])[0]
        slow = (q.get("slow") or ["0"])[0] == "1"
        cards = []
        for slug, title in COURSES:
            if slow:
                a = ('<a href="#" onclick="setTimeout(function(){window.open(\'/learn/%s\',\'_blank\')},1500);return false">%s</a>' % (slug, title))
            else:
                a = '<a href="/learn/%s" target="_blank">%s</a>' % (slug, title)
            cards.append("<article><h3>%s</h3><p>Course</p></article>" % a)
        return page(f"Search results for {term} | CourseHub", COURSE_NAV + f"<main><h1>Results for {term}</h1>{''.join(cards)}</main>")
    if path.startswith("/learn/"):
        slug = path.rsplit("/", 1)[1]
        title = " ".join(w.capitalize() for w in slug.split("-"))
        return page(f"{title} | CourseHub", COURSE_NAV + f"<main><h1>{title}</h1><h2>About this course</h2><p>Learn the basics.</p></main>")
    if path in ("/explore", "/login"):
        return page(path[1:].title(), COURSE_NAV + f"<main><h1>{path[1:].title()}</h1></main>")
    return None


def chat(path, q):
    variant = (q.get("v") or ["editable"])[0]
    head = '<header><a href="/">ChatLike</a> <input type="search" placeholder="Search chats" aria-label="Search chats"></header>'
    sent = "window.__sent=0;"
    if path != "/":
        return None
    if variant == "editable":
        script = (
            sent + "setTimeout(function(){document.getElementById('host').innerHTML="
            "'<div id=\"prompt\" role=\"textbox\" aria-label=\"Message ChatLike\" contenteditable=\"true\"></div>"
            "<button id=\"send\" aria-label=\"Send message\">Send</button>';"
            "document.getElementById('prompt').addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();var t=this.innerText.trim();if(t){window.__sent++;window.__last=t;var m=document.createElement('p');m.className='msg';m.textContent='You: '+t;document.querySelector('main').appendChild(m);this.innerText=''}}});"
            "document.getElementById('send').onclick=function(){window.__sent++}},800)"
        )
        return page("ChatLike", head + '<main><h1>What can I help with?</h1><div id="host"></div></main>', script)
    if variant == "textarea":
        return page("ChatLike", head + '<main><h1>Chat</h1><textarea placeholder="Type your message" rows="3"></textarea><button>Send</button></main>',
                    sent + "document.querySelector('textarea').addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();var t=this.value.trim();if(t){window.__sent++;window.__last=t;var m=document.createElement('p');m.className='msg';m.textContent='You: '+t;document.querySelector('main').appendChild(m);this.value=''}}})")
    if variant == "bare":
        return page("ChatLike", head + '<main><h1>Chat</h1><div contenteditable="true" id="x" style="min-height:40px;border:1px solid #888"></div></main>', sent)
    if variant == "login":
        return page("ChatLike - Log in", head.replace('<input type="search" placeholder="Search chats" aria-label="Search chats">', "")
                    + "<main><h1>Welcome back</h1><button>Log in</button> <button>Sign up</button></main>", sent)
    return None


def hub(path, q):
    slow = (q.get("slow") or ["0"])[0] == "1"
    if path == "/":
        if slow:
            link = '<a href="#" onclick="setTimeout(function(){window.open(\'/partner\',\'_blank\')},1500);return false">Open partner docs</a>'
        else:
            link = '<a href="/partner" target="_blank">Open partner docs</a>'
        return page("Hub", f'<header><a href="/">Hub</a></header><main><h1>Hub</h1><p>Welcome.</p>{link} <a href="/about">About the hub</a></main>')
    if path == "/partner":
        return page("Partner docs", '<header><a href="/partner">Partner</a></header><main><h1>Partner docs</h1><a href="/partner/pricing">Pricing details</a></main>')
    if path == "/partner/pricing":
        return page("Partner pricing", "<main><h1>Partner pricing</h1><h2>Plans</h2><p>Starter and Pro.</p></main>")
    if path == "/about":
        return page("About the hub", "<main><h1>About</h1></main>")
    return None


# ---------------------------------------------------------------------------
# Fixtures for the goal runner (behaviours only - no real site).
#   gsearch  - search-engine-like: home search box, results page with prefilled box + nav chrome,
#              ?trap=1 makes the FIRST result a dead link (wrong-target recovery)
#   vsite    - video-site-like: JS-driven search that does NOT submit on Enter (needs the button)
#   longpage - tall page: "Load more" at the bottom, a dead "Do nothing" button at the top
#   blocked  - human-verification page: ?v=temp clears itself after 3 s, ?v=perm never does
# ---------------------------------------------------------------------------
G_NAV = '<nav aria-label="Search types"><a href="/images">Images</a> <a href="/videos">Videos</a> <a href="/news">News</a></nav>'


def gsearch(path, q):
    if path == "/":
        return page("SearchLike", '<main><h1>SearchLike</h1><form action="/search" method="get" role="search">'
                    '<input type="search" name="q" aria-label="Search" placeholder="Search"><button type="submit">Search</button></form></main>')
    if path == "/search":
        term = (q.get("q") or [""])[0]
        trap = (q.get("trap") or ["0"])[0] == "1"
        results = []
        for n in range(1, 6):
            href = "#" if (trap and n == 1) else f"/page/{n}?q={term.replace(' ', '+')}"
            results.append(f'<div class="g"><a href="{href}"><h3>{term} - result {n}</h3></a><p>Snippet {n} about {term}.</p></div>')
        head = (f'<header><form action="/search" method="get" role="search"><input type="search" name="q" value="{term}" '
                f'aria-label="Search"><button type="submit">Search</button></form>{G_NAV}</header>')
        return page(f"{term} - SearchLike", head + f'<main><h1>Results</h1>{"".join(results)}</main><footer><a href="/privacy">Privacy</a></footer>')
    if path.startswith("/page/"):
        n = path.rsplit("/", 1)[1]
        term = (q.get("q") or [""])[0]
        return page(f"Result {n} for {term}", f"<main><h1>Result page {n}</h1><p>About {term}.</p><h2>Details</h2></main>")
    if path in ("/images", "/videos", "/news", "/privacy"):
        return page(path[1:].title(), f"<main><h1>{path[1:].title()}</h1></main>")
    return None


def vsite(path, q):
    if path == "/":
        return page("VideoLike", '<header><input id="sq" type="text" placeholder="Search videos" aria-label="Search videos">'
                    '<button id="sb" aria-label="Search">Search</button></header><main><h1>VideoLike home</h1></main>',
                    "document.getElementById('sb').onclick=function(){var v=document.getElementById('sq').value;"
                    "if(v){location.href='/results?search_query='+encodeURIComponent(v)}}")
    if path == "/results":
        term = (q.get("search_query") or [""])[0]
        items = "".join(f'<div><a href="/watch?v={n}" id="video-title">{term} video {n}</a></div>' for n in range(1, 5))
        return page(f"{term} - VideoLike", '<header><input id="sq" type="text" placeholder="Search videos" aria-label="Search videos">'
                    f'<button id="sb" aria-label="Search">Search</button></header><main><h1>Results for {term}</h1>{items}</main>')
    if path == "/watch":
        return page("Watching", "<main><h1>Video player</h1></main>")
    return None


def longpage(path, q):
    if path != "/":
        return None
    filler = "".join(f"<p>Paragraph {i}. Lorem ipsum dolor sit amet.</p>" for i in range(1, 90))
    return page(
        "LongPage",
        '<header><button id="noop">Do nothing</button></header><main><h1>Long page</h1>' + filler[: len(filler) // 2]
        + '<h2 id="mid">Middle section</h2>' + filler[len(filler) // 2:]
        + '<button id="more">Load more</button><div id="out"></div></main>',
        "document.getElementById('more').onclick=function(){var h=document.createElement('h2');h.textContent='More items loaded';"
        "document.getElementById('out').appendChild(h);var b=document.createElement('button');b.textContent='Item added';"
        "document.getElementById('out').appendChild(b)}",
    )


def blocked(path, q):
    if path != "/":
        return None
    variant = (q.get("v") or ["temp"])[0]
    script = ("setTimeout(function(){document.getElementById('c').innerHTML='<h1>Welcome</h1><p>Normal content now.</p>';"
              "document.title='Welcome'},3000)" if variant == "temp" else "")
    return page("Just a moment...", '<main id="c"><h1>Verify you are human</h1><p>Our systems detected unusual traffic. '
                'Please complete the CAPTCHA to continue.</p></main>', script)


def forms(path, q):
    """Competing fields: a newsletter e-mail box comes FIRST in the DOM, before the real search / contact fields."""
    news = ('<aside id="nl"><form class="newsletter signup" action="/subscribe"><label>Subscribe to our newsletter'
            '<input type="email" name="nl_email" placeholder="Your email"></label><button>Subscribe</button></form></aside>')
    head = news + '<header><a href="/">FormsLike</a> <nav><a href="/contact">Contact page</a></nav>' \
        '<form role="search" action="/results"><input name="q" type="text" placeholder="Search docs" aria-label="Search docs"><button>Search</button></form></header>'
    if path == "/":
        return page("FormsLike", head + "<main><h1>Welcome</h1><p>Docs home.</p></main>")
    if path == "/results":
        qq = (q.get("q") or [""])[0]
        links = "".join(f'<a href="/doc/{i}">{qq} guide {i}</a><br>' for i in range(1, 4))
        return page(f"Results for {qq}", head + f"<main><h1>Results</h1>{links}</main>")
    if path == "/contact":
        form = ('<main><h1>Contact us</h1><form id="cf" onsubmit="return false"><label>Your name<input name="name" type="text"></label>'
                '<label>Email address<input type="email" name="email"></label>'
                '<label>Message<textarea name="message" aria-label="Message"></textarea></label><button>Send</button></form>'
                '<div id="ed" role="textbox" aria-label="Rich editor" contenteditable="true" style="min-height:30px;border:1px solid #888"></div></main>')
        return page("Contact", head + form)
    return None


def mail(path, q):
    """A webmail-like app (NOT any real site): header search, newsletter e-mail box FIRST in the DOM, a Compose button
    that opens a dialog (To combobox with autocomplete / Cc / Subject / contenteditable body / Send / Discard).

      ?c=two|one|none   contacts matching "Rahul"       ?v=login  sign-in wall       ?v=nocompose  no compose control
      ?v=disabled       compose control disabled         ?send=silent  Send click does nothing
    """
    if path != "/":
        return None
    contacts = {"two": [("Rahul Sharma", "rahul.sharma@example.com"), ("Rahul Verma", "rahul.verma@example.com"), ("Priya Nair", "priya@example.com")],
                "one": [("Rahul Sharma", "rahul.sharma@example.com"), ("Priya Nair", "priya@example.com")],
                "none": [("Priya Nair", "priya@example.com")]}[(q.get("c") or ["one"])[0]]
    variant = (q.get("v") or ["ok"])[0]
    silent = (q.get("send") or [""])[0] == "silent"
    pick = (q.get("pick") or ["ok"])[0]           # pick=noop: clicking a suggestion does nothing (typed text stays, list stays open)
    news = ('<aside><form class="newsletter signup" onsubmit="return false"><label>Subscribe to our newsletter'
            '<input type="email" name="nl_email" placeholder="Your email"></label><button>Subscribe</button></form></aside>')
    head = news + '<header><b>MailLike</b> <input type="search" placeholder="Search mail" aria-label="Search mail"></header>'
    if variant == "login":
        return page("MailLike - Sign in", head.replace('<input type="search" placeholder="Search mail" aria-label="Search mail">', "")
                    + "<main><h1>Sign in to continue</h1><button>Log in</button> <button>Create account</button></main>")
    compose_btn = {"ok": '<button id="compose">Compose</button>', "disabled": '<button id="compose" disabled>Compose</button>',
                   "nocompose": ""}.get(variant, '<button id="compose">Compose</button>')
    cj = ",".join('["%s","%s"]' % c for c in contacts)
    body = head + f"""<main><h1>Inbox</h1>{compose_btn}<div id="banner" role="status"></div>
<div id="dlg" role="dialog" aria-label="New Message" style="display:none;border:1px solid #888;padding:8px">
  <div id="chips"></div>
  <input id="to" role="combobox" aria-label="To recipients" aria-autocomplete="list" autocomplete="off">
  <ul id="lb" role="listbox" style="list-style:none;margin:0;padding:0"></ul>
  <input id="cc" aria-label="Cc">
  <input id="subj" name="subject" placeholder="Subject" aria-label="Subject">
  <div id="body" role="textbox" aria-label="Message Body" contenteditable="true" style="min-height:60px;border:1px solid #aaa"></div>
  <button id="send" aria-label="Send">Send</button> <button id="discard">Discard draft</button>
</div></main>"""
    script = """window.__sent=0;var C=[%s];
var dlg=document.getElementById('dlg'),to=document.getElementById('to'),lb=document.getElementById('lb'),chips=document.getElementById('chips');
var cb=document.getElementById('compose'); if(cb) cb.onclick=function(){dlg.style.display='block'};
function addChip(t){var s=document.createElement('span');s.className='chip';s.textContent=t;chips.appendChild(s);to.value='';lb.innerHTML=''}
to.addEventListener('input',function(){var v=to.value.toLowerCase();lb.innerHTML='';if(!v)return;
  C.filter(function(c){return (c[0]+' '+c[1]).toLowerCase().indexOf(v)>=0}).forEach(function(c){
    var li=document.createElement('li');li.setAttribute('role','option');li.tabIndex=0;li.textContent=c[0]+' <'+c[1]+'>';
    li.onclick=function(){if(%s) return; addChip(c[0]+' <'+c[1]+'>')};lb.appendChild(li)})});
to.addEventListener('keydown',function(e){if((e.key==='Tab'||e.key==='Enter')&&to.value.indexOf('@')>0){addChip(to.value);if(e.key==='Enter')e.preventDefault()}});
document.getElementById('send').onclick=function(){ if(%s) return; window.__sent++; dlg.style.display='none'; document.getElementById('banner').textContent='Message sent'};
document.getElementById('discard').onclick=function(){dlg.style.display='none'};""" % (cj, "true" if pick == "noop" else "false", "true" if silent else "false")
    return page("MailLike", body, script)


def shop(path, q):
    """A store-like site (NOT any real site): header search, a Sign in link, and a sign-in page whose identifier field is
    a combined "Mobile number or email" box.  Variants (?v=): ok | nofield | ambiguous | eat (the page drops typed text)."""
    v = (q.get("v") or ["ok"])[0]
    head = ('<header><b>ShopLike</b> <form role="search" onsubmit="return false"><input type="search" name="k" placeholder="Search products" '
            'aria-label="Search products"></form> <a href="/signin?v=%s">Sign in</a></header>' % v)
    if path == "/":
        return page("ShopLike", head + "<main><h1>Deals</h1><p>Welcome.</p></main>")
    if path == "/signin":
        if v == "nofield":
            return page("Sign in - ShopLike", "<main><h1>Sign in</h1><p>Loading your options...</p></main>")
        if v == "ambiguous":
            fields = ('<label>Mobile phone<input type="tel" name="p1"></label><label>Contact phone<input type="tel" name="p2"></label>')
        else:
            fields = '<label>Mobile number or email<input type="email" name="identifier" id="identifier"></label>'
        script = "window.__submitted=0;document.getElementById('go').onclick=function(){window.__submitted++};"
        if v == "eat":
            script += "document.querySelectorAll('input').forEach(function(i){i.addEventListener('input',function(){setTimeout(function(){i.value=''},50)})});"
        return page("Sign in - ShopLike", head + "<main><h1>Sign in</h1><form onsubmit=\"return false\">" + fields +
                    '<button id="go" type="button">Continue</button></form></main>', script)
    return None


def gmaillike(path, q):
    """A webmail page modelled on the STRUCTURE of Gmail's public DOM (NOT Gmail; nothing here was copied from or tested
    against the live service).  Traits reproduced on purpose:
      * the inbox hydrates late (a splash first) and Compose is a <div role=button>, not a <button>
      * the composer appears ASYNCHRONOUSLY after Compose is clicked (role=dialog "New Message")
      * To = <input role=combobox aria-label="To recipients">, a hidden textarea name=to, chips on Tab/Enter/comma/blur
      * Subject = <input name=subjectbox>, body = <div role=textbox aria-label="Message Body" contenteditable>
      * Send / Discard are <div role=button>; a "Search mail" combobox sits in the header
    Variants:  ?slow=ms (hydration)  ?cdelay=ms (composer delay)  ?v=login | nocomposer | noto | nobody
               ?chip=option (chips are role=option inside a listbox)   ?commit=none (nothing commits a typed address)
               ?sugg=addr (a typed address also shows ONE suggestion row)   ?open=1 (a composer is already open)
               ?c=none|two|one (contacts that match "Rahul")
    """
    if path != "/":
        return None
    g = lambda k, d="": (q.get(k) or [d])[0]
    slow, cdelay, v, chip, commit, sugg, opened = int(g("slow", "700")), int(g("cdelay", "600")), g("v", "ok"), g("chip"), g("commit", "ok"), g("sugg"), g("open")
    contacts = {"one": [("Rahul Sharma", "rahul.sharma@example.com"), ("Priya Nair", "priya@example.com")],
                "two": [("Rahul Sharma", "rahul.sharma@example.com"), ("Rahul Verma", "rahul.verma@example.com")],
                "none": [("Priya Nair", "priya@example.com")]}[g("c", "one")]
    cj = ",".join('["%s","%s"]' % c for c in contacts)
    if v == "login":
        return page("Sign in - MailLike Accounts", "<main><h1>Sign in</h1><label>Email or phone<input type=email name=identifier></label>"
                    "<button>Next</button></main>")
    to_input = "" if v == "noto" else ('<input id="to" class="agP" role="combobox" aria-label="To recipients" aria-haspopup="listbox" '
                                       'aria-autocomplete="list" autocomplete="off" aria-expanded="false">')
    body_div = "" if v == "nobody" else ('<div id="body" role="textbox" aria-label="Message Body" aria-multiline="true" contenteditable="true" '
                                         'g_editable="true" style="min-height:70px;border:1px solid #aaa"></div>')
    dialog = f"""<div id="dlg" role="dialog" aria-label="New Message" style="display:none;border:1px solid #888;padding:8px;margin-top:8px">
  <div role="heading" aria-level="2">New Message</div>
  <div><span>To</span> <span id="chips" role="{'listbox' if chip == 'option' else 'presentation'}" aria-label="Recipients"></span>{to_input}
    <ul id="lb" role="listbox" style="list-style:none;margin:0;padding:0"></ul>
    <textarea name="to" id="hiddento" aria-hidden="true" tabindex="-1" style="display:none"></textarea>
    <span role="link" tabindex="0">Cc</span> <span role="link" tabindex="0">Bcc</span></div>
  <input id="subj" name="subjectbox" placeholder="Subject" aria-label="Subject">
  {body_div}
  <div role="button" id="send" aria-label="Send (Ctrl-Enter)" tabindex="0">Send</div>
  <div role="button" id="discard" aria-label="Discard draft" tabindex="0">Discard</div>
</div>"""
    body = f"""<div id="splash">Loading MailLike…</div>
<div id="app" style="display:none">
<header><span>MailLike</span> <input name="q" role="combobox" aria-label="Search mail" placeholder="Search mail"></header>
<div role="navigation" aria-label="Mail"><div role="button" gh="cm" aria-label="Compose" tabindex="0" id="compose">Compose</div>
  <a href="#inbox">Inbox</a> <a href="#sent">Sent</a></div>
<main><h1>Inbox</h1><div id="banner" role="status"></div>{dialog}</main></div>"""
    script = """window.__sent=0;window.__opens=0;var C=[%s],CHIP=%r,COMMIT=%r,SUGG=%r,V=%r;
var $=function(i){return document.getElementById(i)};
setTimeout(function(){$('splash').style.display='none';$('app').style.display='block'},%d);
function openDlg(){window.__opens++;if($('dlg').style.display==='block')return;setTimeout(function(){ if(V==='nocomposer') return; $('dlg').style.display='block'; if($('to')) $('to').focus()},%d)}
$('compose').onclick=openDlg;
if(%r==='1'){$('dlg').style.display='block'}
var to=$('to'),lb=$('lb'),chips=$('chips');
function addChip(label,email){var s=document.createElement('span');s.className='afV';s.setAttribute('email',email);s.setAttribute('data-hovercard-id',email);
  if(CHIP==='option')s.setAttribute('role','option');s.textContent=label;chips.appendChild(s);to.value='';lb.innerHTML='';to.setAttribute('aria-expanded','false');
  $('hiddento').value=($('hiddento').value?$('hiddento').value+',':'')+email}
function commitTyped(){var t=to.value.trim();if(COMMIT==='none')return;if(/^[^@\\s]+@[^@\\s]+\\.[a-z]{2,}$/i.test(t))addChip(t,t)}
if(to){to.addEventListener('input',function(){var v=to.value.toLowerCase();lb.innerHTML='';to.setAttribute('aria-expanded','false');if(!v)return;
  var list=C.filter(function(c){return (c[0]+' '+c[1]).toLowerCase().indexOf(v)>=0});
  if(SUGG==='addr'&&/@/.test(v))list=[[to.value,to.value]];
  list.forEach(function(c){var li=document.createElement('li');li.setAttribute('role','option');li.tabIndex=0;
    li.textContent=(c[0]===c[1]?c[1]:c[0]+' <'+c[1]+'>');li.onclick=function(){addChip(c[0],c[1])};lb.appendChild(li)});
  if(list.length)to.setAttribute('aria-expanded','true')});
  to.addEventListener('keydown',function(e){if(e.key==='Tab'||e.key==='Enter'||e.key===','){if(to.value.trim()&&COMMIT!=='none'){commitTyped();if(e.key!=='Tab')e.preventDefault()}}});
  to.addEventListener('blur',function(){setTimeout(function(){if(document.activeElement!==to)commitTyped()},120)})}
$('send').onclick=function(){window.__sent++;$('dlg').style.display='none';$('banner').textContent='Message sent'};
$('discard').onclick=function(){$('dlg').style.display='none'};""" % (cj, chip, commit, sugg, v, slow, cdelay, opened)
    return page("MailLike - Inbox", body, script)
