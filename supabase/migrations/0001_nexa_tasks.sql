-- NEXA persistent history: tasks + append-oriented task_events, isolated per user with Row Level Security.
-- Supabase stores what the NEXA backend decided. It never generates actions and never decides success.
-- Apply with the Supabase CLI (`supabase db push`) or paste into the SQL editor. Idempotent where practical.

create table if not exists public.tasks (
  id                  uuid primary key,
  user_id             uuid not null references auth.users (id) on delete cascade default auth.uid(),
  instruction         text not null check (char_length(instruction) between 1 and 4000),
  status              text not null check (status in ('created','running','success','failed','cancelled','timeout',
                                                       'awaiting_confirmation','needs_input')),
  provider            text check (provider is null or char_length(provider) <= 40),        -- the planner that ACTUALLY answered
  result_summary      text check (result_summary is null or char_length(result_summary) <= 1000),
  verification_status text check (verification_status is null or verification_status in
                                  ('PENDING','RUNNING','ACTION_SUCCEEDED','VERIFIED_COMPLETE','BLOCKED','FAILED','CANCELLED')),
  started_at          timestamptz,
  finished_at         timestamptz,
  created_at          timestamptz not null default now(),
  check (finished_at is null or started_at is null or finished_at >= started_at)
);

create table if not exists public.task_events (
  id          uuid primary key,                                        -- deterministic (uuid5 of task + seq): retries cannot duplicate
  task_id     uuid not null references public.tasks (id) on delete cascade,
  user_id     uuid not null references auth.users (id) on delete cascade default auth.uid(),
  event_type  text not null check (char_length(event_type) between 1 and 60),
  message     text check (message is null or char_length(message) <= 400),
  details     jsonb not null default '{}'::jsonb check (pg_column_size(details) <= 16384),
  seq         integer,
  created_at  timestamptz not null default now()
);

create index if not exists tasks_user_created_idx       on public.tasks (user_id, created_at desc);
create index if not exists task_events_task_time_idx    on public.task_events (task_id, created_at, seq);
create index if not exists task_events_user_idx         on public.task_events (user_id);

-- ids and ownership are immutable after insert
create or replace function public.nexa_tasks_immutable() returns trigger
language plpgsql as $$
begin
  if new.id <> old.id or new.user_id <> old.user_id or new.created_at <> old.created_at then
    raise exception 'tasks.id, user_id and created_at are immutable';
  end if;
  return new;
end $$;
drop trigger if exists tasks_immutable on public.tasks;
create trigger tasks_immutable before update on public.tasks for each row execute function public.nexa_tasks_immutable();

-- an event must belong to a task of the SAME user (defence in depth next to the RLS check)
create or replace function public.nexa_event_owner_matches() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if not exists (select 1 from public.tasks t where t.id = new.task_id and t.user_id = new.user_id) then
    raise exception 'task_events.user_id must equal the owner of the task';
  end if;
  return new;
end $$;
drop trigger if exists task_events_owner on public.task_events;
create trigger task_events_owner before insert on public.task_events for each row execute function public.nexa_event_owner_matches();

-- ---- Row Level Security -----------------------------------------------------------------------------------------
alter table public.tasks       enable row level security;
alter table public.task_events enable row level security;
alter table public.tasks       force row level security;
alter table public.task_events force row level security;

drop policy if exists tasks_select on public.tasks;
drop policy if exists tasks_insert on public.tasks;
drop policy if exists tasks_update on public.tasks;
drop policy if exists tasks_delete on public.tasks;
create policy tasks_select on public.tasks for select to authenticated using (user_id = (select auth.uid()));
create policy tasks_insert on public.tasks for insert to authenticated with check (user_id = (select auth.uid()));
create policy tasks_update on public.tasks for update to authenticated using (user_id = (select auth.uid()))
                                                                       with check (user_id = (select auth.uid()));
create policy tasks_delete on public.tasks for delete to authenticated using (user_id = (select auth.uid()));

drop policy if exists task_events_select on public.task_events;
drop policy if exists task_events_insert on public.task_events;
create policy task_events_select on public.task_events for select to authenticated using (user_id = (select auth.uid()));
create policy task_events_insert on public.task_events for insert to authenticated
  with check (user_id = (select auth.uid())
              and exists (select 1 from public.tasks t where t.id = task_id and t.user_id = (select auth.uid())));
-- NO update / delete policy on task_events: the timeline is append-only for clients. Deleting a task removes its events
-- through the foreign key's ON DELETE CASCADE.

-- ---- Grants: nothing for anon / public; least privilege for signed-in users ----------------------------------------
revoke all on public.tasks       from anon, public;
revoke all on public.task_events from anon, public;
grant select, insert, delete on public.tasks to authenticated;
grant update (status, provider, result_summary, verification_status, started_at, finished_at) on public.tasks to authenticated;
grant select, insert on public.task_events to authenticated;
grant all on public.tasks, public.task_events to service_role;

-- ---- Retention: remove a user's history older than N days (run with the service role, e.g. from a scheduled job) -------
create or replace function public.nexa_purge_old_tasks(days integer default 90) returns integer
language plpgsql security definer set search_path = public as $$
declare n integer;
begin
  if days < 1 then raise exception 'days must be >= 1'; end if;
  delete from public.tasks where created_at < now() - make_interval(days => days);
  get diagnostics n = row_count;
  return n;
end $$;
revoke all on function public.nexa_purge_old_tasks(integer) from public, anon, authenticated;
grant execute on function public.nexa_purge_old_tasks(integer) to service_role;
