-- Supabase publishes tables in the public schema through its REST API
-- (PostgREST), reachable with the project's anon key. Row-level security
-- with no policies blocks that API completely. The app is unaffected: it
-- connects as the tables' owner, which RLS doesn't apply to.
--
-- Covers every table in the schema, so re-run it after adding a table.
-- Harmless on a plain Postgres database such as the local Docker one.
DO $$
DECLARE
    t record;
BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.tablename);
    END LOOP;
END $$;
