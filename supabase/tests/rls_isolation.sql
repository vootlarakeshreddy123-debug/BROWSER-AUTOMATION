-- RLS isolation test for NEXA. Run against a DISPOSABLE Supabase/Postgres database AFTER applying the migration:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f supabase/tests/rls_isolation.sql
-- It simulates two signed-in users by setting the role and the JWT claims, and RAISES on any isolation failure.
-- It rolls everything back at the end. (Status in the delivered report: NOT RUN - no Postgres in the build sandbox.)
begin;

insert into auth.users (id, instance_id, aud, role, email) values
  ('11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'alice@example.test'),
  ('22222222-2222-2222-2222-222222222222', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'bob@example.test');

create or replace function pg_temp.act_as(uid uuid) returns void language plpgsql as $$
begin
  perform set_config('request.jwt.claims', json_build_object('sub', uid, 'role', 'authenticated')::text, true);
  perform set_config('request.jwt.claim.sub', uid::text, true);
  set local role authenticated;
end $$;

-- Alice writes her own task + event
select pg_temp.act_as('11111111-1111-1111-1111-111111111111');
insert into public.tasks (id, instruction, status) values ('aaaaaaaa-0000-0000-0000-00000000000a', 'alice task', 'running');
insert into public.task_events (id, task_id, event_type) values ('eeeeeeee-0000-0000-0000-00000000000a', 'aaaaaaaa-0000-0000-0000-00000000000a', 'task_start');

do $$ begin
  if (select count(*) from public.tasks) <> 1 then raise exception 'FAIL: alice should see exactly her own task'; end if;
end $$;

-- Bob sees nothing of Alice's
reset role;
select pg_temp.act_as('22222222-2222-2222-2222-222222222222');
do $$
declare n int;
begin
  if (select count(*) from public.tasks) <> 0 then raise exception 'FAIL: bob can read alice tasks'; end if;
  if (select count(*) from public.task_events) <> 0 then raise exception 'FAIL: bob can read alice events'; end if;

  update public.tasks set status = 'failed' where id = 'aaaaaaaa-0000-0000-0000-00000000000a';
  get diagnostics n = row_count;
  if n <> 0 then raise exception 'FAIL: bob updated alice task'; end if;

  delete from public.tasks where id = 'aaaaaaaa-0000-0000-0000-00000000000a';
  get diagnostics n = row_count;
  if n <> 0 then raise exception 'FAIL: bob deleted alice task'; end if;

  begin
    insert into public.tasks (id, user_id, instruction, status) values ('bbbbbbbb-0000-0000-0000-00000000000b', '11111111-1111-1111-1111-111111111111', 'spoof', 'running');
    raise exception 'FAIL: bob inserted a task owned by alice';
  exception when insufficient_privilege or check_violation then null; end;

  begin
    insert into public.task_events (id, task_id, event_type) values ('eeeeeeee-0000-0000-0000-00000000000b', 'aaaaaaaa-0000-0000-0000-00000000000a', 'spoof');
    raise exception 'FAIL: bob appended an event to alice task';
  exception when insufficient_privilege or check_violation or raise_exception then
    if sqlerrm like 'FAIL:%' then raise; end if;
  end;

  begin
    update public.task_events set message = 'x';
    raise exception 'FAIL: clients may not update events';
  exception when insufficient_privilege then null; end;
end $$;

-- anon sees nothing and cannot write
reset role;
set local role anon;
do $$ begin
  begin
    perform count(*) from public.tasks;
    raise exception 'FAIL: anon can query tasks';
  exception when insufficient_privilege then null; end;
end $$;

-- Alice cannot change ownership
reset role;
select pg_temp.act_as('11111111-1111-1111-1111-111111111111');
do $$ begin
  begin
    update public.tasks set user_id = '22222222-2222-2222-2222-222222222222';
    raise exception 'FAIL: alice reassigned ownership';
  exception when insufficient_privilege then null; end;
end $$;

select 'RLS isolation: ALL CHECKS PASSED' as result;
rollback;
