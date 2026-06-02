-- 011_delete_my_account_rpc.sql
--
-- Self-service account deletion for Apple Guideline 5.1.1(v).
--
-- A SECURITY DEFINER RPC the authenticated user calls to permanently delete
-- their OWN account. It deletes the caller's auth.users row (resolved from
-- auth.uid(), so a user can never delete anyone else), which cascades to every
-- app table via the existing ON DELETE CASCADE foreign keys verified on
-- 2026-06-02: entitlements, user_entitlements, user_trip_counts, saved_places,
-- roam_plans, roam_plan_members, emergency_contacts, stop_memories,
-- public_trips, public_trip_clones, plus the auth-schema children (identities,
-- sessions, mfa_factors, one_time_tokens, oauth_*, webauthn_*).
--
-- Runs as the function owner (postgres) so it has privilege on auth.users.
-- This replaces the stale Cloud Run DELETE /account path for the native iOS
-- app, which could not be redeployed (gcloud auth blocked). The native client
-- calls it as `client.rpc("delete_my_account")`.

create or replace function public.delete_my_account()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  uid uuid := auth.uid();
begin
  if uid is null then
    raise exception 'not authenticated';
  end if;
  delete from auth.users where id = uid;
  return jsonb_build_object('deleted', true);
end;
$$;

revoke all on function public.delete_my_account() from public;
revoke all on function public.delete_my_account() from anon;
grant execute on function public.delete_my_account() to authenticated;
