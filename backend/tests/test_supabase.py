"""Supabase auth + persistence tests against an in-memory GoTrue/PostgREST emulator (no network, no Supabase project).

The emulator enforces the same ownership rules the SQL policies enforce (a user sees / changes only their rows).  The REAL
policies are tested by supabase/tests/rls_isolation.sql, which needs a Postgres database (NOT RUN in the build sandbox).
Run: python -m unittest tests.test_supabase -v   (from backend/)"""
import asyncio, base64, json, sys, time, unittest
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.append(str(HERE / "_shims"))

from agent import events as ev
from database.models import TaskRecord, TaskStatus
from database.repository import TaskRepository
from services.auth import LOCAL_USER, authenticate, extract_bearer, owns
from services.persistence import PersistenceManager, event_id, event_row
from services.supabase_service import AuthError, AuthUnavailable, AuthUser, SupabaseError, SupabaseService

PUB, SECRET = "sb_publishable_TESTPUBLISHABLEKEY123", "sb_secret_TESTSECRETKEY1234567890"
URL = "https://proj.supabase.test"


def jwt(sub, exp=None):
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b({'alg': 'HS256'})}.{b({'sub': sub, 'exp': exp or time.time() + 3600})}.sig{sub}"


ALICE, BOB = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
TA, TB = jwt("alice"), jwt("bob")


class Emulator:
    def __init__(self):
        self.users = {TA: ALICE, TB: BOB}
        self.expired: set[str] = set()
        self.tasks: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.down = False
        self.fail: list[tuple[int, str]] = []        # queued (status, body) failures for REST calls
        self.auth_status: int | None = None
        self.privileged_reads = 0

    async def __call__(self, method, url, headers, params, body, timeout):
        self.calls.append((method, url))
        if self.down:
            raise ConnectionError("connection refused " + headers.get("Authorization", ""))
        path = url.replace(URL, "")
        if path == "/auth/v1/user":
            if self.auth_status:
                return self.auth_status, {"msg": "x"}, {}
            tok = headers["Authorization"].split()[1]
            if headers["apikey"] != PUB or tok not in self.users or tok in self.expired:
                return 401, {"msg": "invalid JWT"}, {}
            return 200, {"id": self.users[tok], "email": "e@x.test"}, {}
        if self.fail:
            st, msg = self.fail.pop(0)
            return st, {"message": msg}, {}
        bearer = headers["Authorization"].split()[1]
        privileged = bearer == SECRET
        uid = None
        if not privileged:
            if bearer in self.expired or bearer not in self.users:
                return 401, {"message": "JWT expired"}, {}
            uid = self.users[bearer]
        table = path.split("/rest/v1/")[1]
        store = self.tasks if table == "tasks" else self.events
        ignore = "ignore-duplicates" in headers.get("Prefer", "")
        if method == "POST":
            rows = body if isinstance(body, list) else [body]
            for r in rows:
                if not privileged and r.get("user_id") != uid:
                    return 403, {"message": "new row violates row-level security policy"}, {}
                if table == "task_events":
                    parent = self.tasks.get(r["task_id"])
                    if not parent or parent["user_id"] != r["user_id"]:
                        return 403, {"message": "new row violates row-level security policy"}, {}
                if r["id"] in store:
                    if ignore:
                        continue
                    return 409, {"message": "duplicate key"}, {}
                store[r["id"]] = dict(r)
            return 201, None, {}
        if privileged and method == "GET":
            self.privileged_reads += 1
        visible = [r for r in store.values() if privileged or r["user_id"] == uid]
        for k, v in (params or {}).items():
            if v.startswith("eq."):
                visible = [r for r in visible if str(r.get(k)) == v[3:]]
        if method == "PATCH":
            for r in visible:
                r.update(body)
            return 204, None, {}
        if method == "GET":
            key = (lambda r: (r.get("created_at", ""), r.get("seq") or 0))
            visible.sort(key=key, reverse="desc" in (params or {}).get("order", ""))
            return 200, [dict(r) for r in visible][: int((params or {}).get("limit", 100))], {}
        return 405, {}, {}


def svc(em, secret=SECRET, cache=30.0):
    return SupabaseService(URL, PUB, secret, transport=em, cache_ttl_s=cache)


def task(owner=ALICE, instruction="open example.com", status=TaskStatus.RUNNING):
    t = TaskRecord(instruction=instruction, owner_id=owner)
    t.status = status
    return t


async def nosleep(_): return None


def mgr(em, secret=SECRET):
    m = PersistenceManager()
    m.configure(svc(em, secret), True, sleep=nosleep)
    return m


def run(c): return asyncio.run(c)


async def _settle(m, *fns):
    for f in fns:
        f()
    await m.drain()


def settle(m, *fns):
    """Run the (non-blocking) persistence hooks inside an event loop - as the server does - and wait for the writes."""
    run(_settle(m, *fns))


class TokenValidation(unittest.TestCase):
    def test_valid_token_resolves_user_from_the_auth_server(self):
        u = run(svc(Emulator()).validate_token(TA)); self.assertEqual(u.id, ALICE)

    def test_missing_malformed_invalid_expired_are_rejected(self):
        em = Emulator(); s = svc(em)
        for bad in (None, "", "abc", "a.b", jwt("mallory")):
            with self.assertRaises(AuthError) as cm: run(s.validate_token(bad))
            self.assertEqual(cm.exception.status, 401)
        em.expired.add(TA)
        with self.assertRaises(AuthError): run(s.validate_token(TA))

    def test_unreachable_auth_service_fails_closed(self):
        em = Emulator(); em.down = True
        with self.assertRaises(AuthUnavailable) as cm: run(svc(em).validate_token(TA))
        self.assertEqual(cm.exception.status, 503)
        em.down = False; em.auth_status = 500
        with self.assertRaises(AuthUnavailable): run(svc(em).validate_token(TA))

    def test_cache_avoids_repeat_calls_but_never_outlives_the_token(self):
        em = Emulator(); s = svc(em); run(s.validate_token(TA)); run(s.validate_token(TA))
        self.assertEqual(sum(1 for c in em.calls if c[1].endswith("/auth/v1/user")), 1)
        short = jwt("alice", exp=time.time() + 0.0)               # already expired by the token's own claim -> not cached
        em.users[short] = ALICE; em2 = svc(em)
        run(em2.validate_token(short)); run(em2.validate_token(short))
        self.assertEqual(sum(1 for c in em.calls if c[1].endswith("/auth/v1/user")), 3)

    def test_unconfigured_service_denies(self):
        with self.assertRaises(AuthUnavailable): run(SupabaseService("", None, transport=Emulator()).validate_token(TA))

    def test_extract_bearer(self):
        self.assertEqual(extract_bearer("Bearer abc"), "abc"); self.assertEqual(extract_bearer("bearer  abc"), "abc")
        for bad in (None, "", "Basic abc", "Bearer", "abc"): self.assertIsNone(extract_bearer(bad))


class Authorisation(unittest.TestCase):
    def test_disabled_mode_is_local_single_user(self):
        u, tok = run(authenticate(None, None, False)); self.assertEqual((u, tok), (LOCAL_USER, None))
        self.assertTrue(owns(task(owner=None), LOCAL_USER))

    def test_enabled_without_header_is_401_and_without_service_is_503(self):
        with self.assertRaises(AuthError) as cm: run(authenticate(None, svc(Emulator()), True))
        self.assertEqual(cm.exception.status, 401)
        with self.assertRaises(AuthUnavailable): run(authenticate(f"Bearer {TA}", None, True))

    def test_owner_comes_from_the_token_not_from_anything_the_client_sends(self):
        u, _ = run(authenticate(f"Bearer {TA}", svc(Emulator()), True))
        self.assertEqual(u.id, ALICE)                       # a body/query 'user_id' has no parameter to travel through
        self.assertTrue(owns(task(owner=ALICE), u)); self.assertFalse(owns(task(owner=BOB), u)); self.assertFalse(owns(task(owner=None), u))

    def test_repository_history_is_scoped(self):
        r = TaskRepository(); a, b = task(ALICE), task(BOB); r.create(a); r.create(b)
        self.assertEqual([t.id for t in r.list_recent(10, owner_id=ALICE, scoped=True)], [a.id])
        self.assertEqual({t.id for t in r.list_recent(10)}, {a.id, b.id})        # local mode: everything
        self.assertEqual(r.list_recent(10, owner_id="nobody", scoped=True), [])


class OwnHistory(unittest.TestCase):
    def finished(self, **kw):
        t = task(**kw); t.provider_used = "groq"
        rec = ev.ExecutionRecorder(t.id); rec.record(ev.TASK_START, instruction=t.instruction)
        rec.record(ev.GOAL_START, label="open https://example.com"); rec.record(ev.VERIFICATION, scope="final", result="complete", verdict="VERIFIED_COMPLETE")
        t.events = rec.events
        t.mark_finished(TaskStatus.SUCCESS, message="done")
        t.verification_status = ev.derive_verification("success", t.events)
        return t

    def test_create_then_view_own_task_and_events_in_order(self):
        em = Emulator(); m = mgr(em); t = self.finished()
        m.bind(t.id, AuthUser(ALICE), TA); settle(m, lambda: m.on_created(t)); settle(m, lambda: m.on_terminal(t))
        s = svc(em)
        rows = run(s.list_tasks(TA)); self.assertEqual([r["id"] for r in rows], [t.id])
        self.assertEqual(rows[0]["status"], "success"); self.assertEqual(rows[0]["verification_status"], "VERIFIED_COMPLETE")
        self.assertEqual(rows[0]["provider"], "groq")                                   # the ACTUAL planner
        evs = run(s.list_events(TA, t.id)); self.assertEqual([e["seq"] for e in evs], sorted(e["seq"] for e in evs))
        self.assertEqual(len(evs), 3); self.assertEqual(evs[0]["event_type"], "task_start")
        self.assertEqual(t.persistence["status"], "saved")

    def test_frontend_status_backend_status_and_stored_row_agree(self):
        em = Emulator(); m = mgr(em); t = self.finished(); m.bind(t.id, AuthUser(ALICE), TA); settle(m, lambda: m.on_terminal(t))
        row = em.tasks[t.id]
        self.assertEqual((row["status"], row["verification_status"]), (t.status.value, t.verification_status))

    def test_sensitive_values_are_not_stored(self):
        em = Emulator(); m = mgr(em)
        t = self.finished(instruction="fill the phone number as 9010635950 and the password is hunter2 mail a@b.co")
        t.message = "typed 9010635950, token Bearer abcdefghijklmnop1234"
        t.events.append({"seq": 99, "type": "ACTION_START", "ts": time.time(), "text": "9010635950", "typed_text": "9010635950",
                         "reason": f"key {SECRET} and sk-abcdefghijklmnopqrstuvwxyz", "password": "hunter2"})
        m.bind(t.id, AuthUser(ALICE), TA); settle(m, lambda: m.on_terminal(t))
        blob = json.dumps([em.tasks, em.events])
        for secret in ("9010635950", "hunter2", "a@b.co", SECRET, "sk-abcdefghij", "abcdefghijklmnop1234", PUB, TA):
            self.assertNotIn(secret, blob, secret)

    def test_owner_is_forced_to_the_validated_user_even_if_row_says_otherwise(self):
        em = Emulator(); s = svc(em)
        run(s.insert_task(BOB, TB, {"id": "t-1", "user_id": ALICE, "instruction": "x", "status": "running"}))
        self.assertEqual(em.tasks["t-1"]["user_id"], BOB)


class CrossUserIsolation(unittest.TestCase):
    def setUp(self):
        self.em = Emulator(); self.m = mgr(self.em); self.t = OwnHistory().finished()
        self.m.bind(self.t.id, AuthUser(ALICE), TA); settle(self.m, lambda: self.m.on_terminal(self.t)); self.s = svc(self.em)

    def test_bob_cannot_read_alices_tasks_or_events(self):
        self.assertEqual(run(self.s.list_tasks(TB)), [])
        self.assertIsNone(run(self.s.get_task(TB, self.t.id)))
        self.assertEqual(run(self.s.list_events(TB, self.t.id)), [])

    def test_bob_cannot_update_alices_task(self):
        run(self.s.update_task(BOB, TB, self.t.id, {"status": "failed"}))
        self.assertEqual(self.em.tasks[self.t.id]["status"], "success")

    def test_bob_cannot_append_events_to_alices_task(self):
        with self.assertRaises(SupabaseError) as cm:
            run(self.s.insert_events(BOB, TB, [{"id": event_id(self.t.id, 500), "task_id": self.t.id, "event_type": "spoof"}]))
        self.assertEqual(cm.exception.status, 403); self.assertFalse(cm.exception.transient)

    def test_reads_never_fall_back_to_the_privileged_key(self):
        self.em.expired.add(TB)
        with self.assertRaises(SupabaseError): run(self.s.list_tasks(TB))
        self.assertEqual(self.em.privileged_reads, 0)            # the emulator never saw a secret-key read

    def test_no_delete_surface_exists_on_the_service(self):
        self.assertFalse(hasattr(self.s, "delete_task")); self.assertFalse(hasattr(self.s, "delete_events"))


class Outage(unittest.TestCase):
    def test_outage_never_claims_saved_and_never_blocks(self):
        em = Emulator(); em.down = True; m = mgr(em); t = OwnHistory().finished(); m.bind(t.id, AuthUser(ALICE), TA)
        async def go():
            m.on_terminal(t)                                          # returns at once: the browser loop is not delayed
            self.assertEqual(t.persistence["status"], "pending")      # and it does not claim "saved" yet
            await m.drain()
        run(go())
        self.assertEqual(t.persistence["status"], "failed"); self.assertIn("network", t.persistence["detail"])
        self.assertEqual(em.tasks, {}); self.assertEqual(em.events, {})

    def test_bounded_retry_then_recovery_without_duplicates(self):
        em = Emulator(); em.down = True; m = mgr(em); t = OwnHistory().finished(); m.bind(t.id, AuthUser(ALICE), TA)
        async def go():
            m.on_terminal(t); await m.drain()
            n_calls = len(em.calls)
            self.assertLessEqual(n_calls, 3)                           # MAX_ATTEMPTS, then it stops
            em.down = False
            self.assertEqual(await m.retry_failed(), 1)
        run(go())
        self.assertEqual(t.persistence["status"], "failed")           # honest until the owner hook runs again
        self.assertEqual(len(em.tasks), 1); self.assertEqual(len(em.events), 3)
        settle(m, lambda: m.on_terminal(t))                              # repeating the hook duplicates nothing
        self.assertEqual((len(em.tasks), len(em.events)), (1, 3)); self.assertEqual(t.persistence["status"], "saved")

    def test_transient_server_errors_are_retried_then_succeed_once(self):
        em = Emulator(); em.fail = [(503, "unavailable"), (503, "unavailable")]; m = mgr(em); t = OwnHistory().finished()
        m.bind(t.id, AuthUser(ALICE), TA); settle(m, lambda: m.on_terminal(t))
        self.assertEqual((len(em.tasks), len(em.events)), (1, 3)); self.assertEqual(t.persistence["status"], "saved")

    def test_permanent_errors_are_not_retried(self):
        em = Emulator(); em.fail = [(400, "bad request")]; m = mgr(em); t = OwnHistory().finished()
        m.bind(t.id, AuthUser(ALICE), TA); settle(m, lambda: m.on_terminal(t))
        self.assertEqual(sum(1 for c in em.calls if "/rest/v1/" in c[1]), 1); self.assertEqual(t.persistence["status"], "failed")

    def test_expired_user_token_finishes_write_with_secret_key_scoped_to_owner(self):
        em = Emulator(); m = mgr(em); t = OwnHistory().finished(); m.bind(t.id, AuthUser(ALICE), TA)
        em.expired.add(TA); settle(m, lambda: m.on_terminal(t))
        self.assertEqual(em.tasks[t.id]["user_id"], ALICE); self.assertEqual(t.persistence["status"], "saved")

    def test_expired_token_without_secret_key_is_reported_failed(self):
        em = Emulator(); m = mgr(em, secret=None); t = OwnHistory().finished(); m.bind(t.id, AuthUser(ALICE), TA)
        em.expired.add(TA); settle(m, lambda: m.on_terminal(t))
        self.assertEqual(em.tasks, {}); self.assertEqual(t.persistence["status"], "failed")

    def test_refreshed_token_is_used_for_later_writes(self):
        em = Emulator(); m = mgr(em, secret=None); t = OwnHistory().finished(); m.bind(t.id, AuthUser(ALICE), TA)
        fresh = jwt("alice-2"); em.users[fresh] = ALICE; m.refresh_token(t.id, fresh)
        em.expired.add(TA); settle(m, lambda: m.on_terminal(t))
        self.assertEqual(t.persistence["status"], "saved")

    def test_disabled_persistence_is_a_noop(self):
        em = Emulator(); m = PersistenceManager(); t = OwnHistory().finished(); settle(m, lambda: m.on_created(t), lambda: m.on_terminal(t))
        self.assertEqual(em.calls, []); self.assertEqual(t.persistence["status"], "disabled")

    def test_local_user_is_never_persisted(self):
        em = Emulator(); m = mgr(em); t = OwnHistory().finished(owner=None); m.bind(t.id, LOCAL_USER, None); settle(m, lambda: m.on_terminal(t))
        self.assertEqual(em.calls, []); self.assertEqual(t.persistence["status"], "disabled")


class RowShape(unittest.TestCase):
    def test_event_ids_are_deterministic_and_rows_clipped(self):
        self.assertEqual(event_id("t", 5), event_id("t", 5)); self.assertNotEqual(event_id("t", 5), event_id("t", 6))
        r = event_row("t", {"seq": 2, "type": "OBSERVE", "ts": 1.0, "reason": "x" * 5000, "url": "https://x.test"})
        self.assertLessEqual(len(r["message"]), 400); self.assertEqual(r["event_type"], "observe"); self.assertEqual(r["seq"], 2)


if __name__ == "__main__":
    unittest.main()
