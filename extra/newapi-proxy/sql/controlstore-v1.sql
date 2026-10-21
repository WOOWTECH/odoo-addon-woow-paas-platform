-- ADMIN ONLY, in a DEDICATED control database, never the upstream application DB.
-- Install once, transactionally, after SET ROLE to a dedicated NOLOGIN owner
-- with NOSUPERUSER NOCREATEDB NOCREATEROLE (temporarily grant database CREATE).
-- No global roles are created here. The DBA owns role provisioning and seeding.
-- After install revoke database CREATE from the owner and PUBLIC; revoke PUBLIC
-- database CONNECT/TEMP, then grant CONNECT to the intended runtime login roles.
-- DBA also REVOKEs CREATE ON SCHEMA public FROM PUBLIC and ensures runtime roles
-- inherit no CREATE/TEMP or elevated privileges from any other database grants.
-- Runtime roles must be NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
-- NOBYPASSRLS and must have NO membership in the owner/admin/other runtime roles.
-- Never grant runtime CREATE on any schema, table privileges, or owner membership.
-- DBA seeds: rate_scope(scope), environment(env), role_binding(role_name,env,scope).
-- Scope is shared by ALL proxy environments/roles using the same upstream budget.
-- Runtime grants ONLY (replace runtime_role with an administrator-chosen role):
-- GRANT USAGE ON SCHEMA proxy_control TO runtime_role;
-- GRANT EXECUTE ON FUNCTION proxy_control.acquire(text,text),
--   proxy_control.begin_attempt(text,text,integer,integer),
--   proxy_control.finish_attempt(text,text,text,integer,text),
--   proxy_control.read_events(text,bigint,bigint,integer),
--   proxy_control.ping(text) TO runtime_role;
-- No runtime DDL, raw table access, DELETE, or default EXECUTE privilege.
-- No automatic retention: retain audit >=180 days; later DBA policy required.
-- Reservations are never refunded, including HTTP not sent. DB reservation
-- timestamps bound the 60s window; actual HTTP starts have the caller's bounded
-- reservation-to-HTTP gap and can straddle that boundary differently.
BEGIN;
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = current_user
               AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolcanlogin)) THEN
        RAISE EXCEPTION 'dedicated nonlogin owner required';
    END IF;
END $$;
CREATE SCHEMA proxy_control;
REVOKE ALL ON SCHEMA proxy_control FROM PUBLIC;
-- PostgreSQL's schema-local REVOKE cannot remove the built-in, GLOBAL PUBLIC
-- EXECUTE default for functions. This dedicated owner has no other schemas.
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE proxy_control.schema_version (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    version integer NOT NULL CHECK (version = 1)
);
INSERT INTO proxy_control.schema_version VALUES (true, 1);
CREATE TABLE proxy_control.rate_scope (
    scope text PRIMARY KEY CHECK (scope ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'),
    last_clock timestamptz,
    slots timestamptz[] NOT NULL DEFAULT '{}' CHECK (cardinality(slots) <= 60)
);
CREATE TABLE proxy_control.environment (
    env text PRIMARY KEY CHECK (env ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'),
    highwater bigint NOT NULL DEFAULT 0 CHECK (highwater >= 0)
);
CREATE TABLE proxy_control.role_binding (
    role_name name PRIMARY KEY,
    env text NOT NULL REFERENCES proxy_control.environment(env),
    scope text NOT NULL REFERENCES proxy_control.rate_scope(scope)
);
CREATE TABLE proxy_control.event (
    env text NOT NULL REFERENCES proxy_control.environment(env),
    seq bigint NOT NULL CHECK (seq > 0),
    attempt_id text NOT NULL CHECK (attempt_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
    kind text NOT NULL CHECK (kind IN ('intent', 'outcome')),
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    target_user_id integer,
    quota integer,
    result text,
    status integer CHECK (status BETWEEN 100 AND 599),
    request_id text CHECK (request_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
      AND request_id !~* 'nap_|sk-|bearer|[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|[0-9a-f]{64}'),
    PRIMARY KEY (env, seq),
    UNIQUE (env, attempt_id, kind),
    CHECK ((kind = 'intent' AND target_user_id > 0 AND quota BETWEEN 1 AND 156250000
            AND target_user_id IS NOT NULL AND quota IS NOT NULL
            AND result IS NULL AND status IS NULL AND request_id IS NULL)
        OR (kind = 'outcome' AND target_user_id IS NULL AND quota IS NULL
            AND result IS NOT NULL AND result IN ('forwarded','connect','reset','timeout','cancelled','not_sent')
            AND ((result = 'forwarded' AND status IS NOT NULL)
              OR (result <> 'forwarded' AND status IS NULL AND request_id IS NULL))))
);

-- Private helper is never granted to runtime; session_user survives SET ROLE.
CREATE FUNCTION proxy_control.binding(p_env text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, proxy_control AS $$
DECLARE v_scope text;
BEGIN
    IF NOT EXISTS (SELECT FROM proxy_control.schema_version WHERE singleton AND version = 1)
       OR p_env IS NULL OR p_env !~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'
       OR EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = session_user
                  AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)) THEN
        RAISE EXCEPTION 'control rejected';
    END IF;
    SELECT b.scope INTO v_scope FROM proxy_control.role_binding b
      JOIN proxy_control.environment e ON e.env = b.env
      JOIN proxy_control.rate_scope s ON s.scope = b.scope
      WHERE b.role_name = session_user AND b.env = p_env;
    IF NOT FOUND THEN RAISE EXCEPTION 'control rejected'; END IF;
    IF EXISTS (SELECT FROM proxy_control.rate_scope s WHERE s.scope = v_scope AND
        ((s.last_clock IS NOT NULL AND NOT isfinite(s.last_clock))
         OR (cardinality(s.slots) > 0 AND s.last_clock IS NULL)
         OR EXISTS (SELECT FROM unnest(s.slots) t WHERE t IS NULL OR NOT isfinite(t) OR t > s.last_clock))) THEN
        RAISE EXCEPTION 'control rejected';
    END IF;
    RETURN v_scope;
END $$;

CREATE FUNCTION proxy_control.acquire(p_env text, p_priority text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, proxy_control AS $$
DECLARE v_scope text; v_last timestamptz; v_slots timestamptz[];
        v_now timestamptz; v_cap integer; v_count integer; v_retry integer;
BEGIN
    v_scope := proxy_control.binding(p_env);
    v_cap := CASE p_priority WHEN 'conversion' THEN 60 WHEN 'free_meter' THEN 50
                 WHEN 'other' THEN 40 WHEN 'balance' THEN 30 ELSE NULL END;
    IF v_cap IS NULL THEN RAISE EXCEPTION 'control rejected'; END IF;
    SELECT last_clock, slots INTO v_last, v_slots FROM proxy_control.rate_scope
      WHERE scope = v_scope FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'control rejected'; END IF;
    v_now := clock_timestamp(); -- after lock acquisition, never transaction start
    IF NOT isfinite(v_now) OR (v_last IS NOT NULL AND NOT isfinite(v_last))
       OR EXISTS (SELECT FROM unnest(v_slots) t WHERE t IS NULL OR NOT isfinite(t))
       OR (cardinality(v_slots) > 0 AND v_last IS NULL)
       OR EXISTS (SELECT FROM unnest(v_slots) t WHERE t > v_last) THEN
        RAISE EXCEPTION 'control rejected';
    END IF;
    -- A backwards clock never expires reservations. Cool down until the DB
    -- catches its last observed clock, then still apply the ordinary window.
    IF v_now < v_last THEN RETURN 60; END IF;
    SELECT coalesce(array_agg(t ORDER BY t), '{}'::timestamptz[]) INTO v_slots
      FROM unnest(v_slots) t WHERE t > v_now - interval '60 seconds';
    v_count := cardinality(v_slots);
    UPDATE proxy_control.rate_scope SET last_clock = v_now, slots = v_slots WHERE scope = v_scope;
    IF v_count >= v_cap THEN
        -- Enough slots must expire to leave total usage below this priority cap.
        v_retry := ceil(extract(epoch FROM (v_slots[v_count-v_cap+1] + interval '60 seconds' - v_now)))::integer;
        RETURN greatest(1, least(60, v_retry));
    END IF;
    UPDATE proxy_control.rate_scope SET slots = array_append(v_slots, v_now) WHERE scope = v_scope;
    RETURN 0;
END $$;

CREATE FUNCTION proxy_control.begin_attempt(p_env text, p_attempt text, p_user integer, p_quota integer)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, proxy_control AS $$
DECLARE v_head bigint;
BEGIN
    PERFORM proxy_control.binding(p_env);
    IF p_attempt IS NULL OR p_attempt !~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
       OR p_user IS NULL OR p_user <= 0 OR p_quota IS NULL OR p_quota NOT BETWEEN 1 AND 156250000 THEN
        RAISE EXCEPTION 'control rejected';
    END IF;
    SELECT highwater INTO v_head FROM proxy_control.environment WHERE env = p_env FOR UPDATE;
    IF NOT FOUND OR v_head = 9223372036854775807 THEN RAISE EXCEPTION 'control rejected'; END IF;
    IF EXISTS (SELECT FROM proxy_control.event WHERE env = p_env AND attempt_id = p_attempt) THEN
        RAISE EXCEPTION 'control rejected'; -- NEVER replay a begin, even identical
    END IF;
    v_head := v_head + 1;
    INSERT INTO proxy_control.event(env,seq,attempt_id,kind,target_user_id,quota)
      VALUES (p_env,v_head,p_attempt,'intent',p_user,p_quota);
    UPDATE proxy_control.environment SET highwater = v_head WHERE env = p_env;
    RETURN v_head;
END $$;

CREATE FUNCTION proxy_control.finish_attempt(p_env text, p_attempt text, p_result text,
                                             p_status integer, p_request text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, proxy_control AS $$
DECLARE v_head bigint; v_existing proxy_control.event%ROWTYPE;
BEGIN
    PERFORM proxy_control.binding(p_env);
    IF p_attempt IS NULL OR p_attempt !~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
       OR p_result IS NULL OR p_result NOT IN ('forwarded','connect','reset','timeout','cancelled','not_sent')
       OR (p_status IS NOT NULL AND p_status NOT BETWEEN 100 AND 599)
       OR (p_request IS NOT NULL AND (p_request !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
           OR p_request ~* 'nap_|sk-|bearer|[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|[0-9a-f]{64}'))
       OR (p_result = 'forwarded' AND p_status IS NULL)
       OR (p_result <> 'forwarded' AND (p_status IS NOT NULL OR p_request IS NOT NULL)) THEN
        RAISE EXCEPTION 'control rejected';
    END IF;
    SELECT highwater INTO v_head FROM proxy_control.environment WHERE env = p_env FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'control rejected'; END IF;
    IF NOT EXISTS (SELECT FROM proxy_control.event WHERE env = p_env AND attempt_id = p_attempt AND kind = 'intent') THEN
        RAISE EXCEPTION 'control rejected';
    END IF;
    SELECT * INTO v_existing FROM proxy_control.event WHERE env = p_env AND attempt_id = p_attempt AND kind = 'outcome';
    IF FOUND THEN
        IF v_existing.result IS NOT DISTINCT FROM p_result AND v_existing.status IS NOT DISTINCT FROM p_status
           AND v_existing.request_id IS NOT DISTINCT FROM p_request THEN RETURN; END IF;
        RAISE EXCEPTION 'control rejected';
    END IF;
    IF v_head = 9223372036854775807 THEN RAISE EXCEPTION 'control rejected'; END IF;
    v_head := v_head + 1;
    INSERT INTO proxy_control.event(env,seq,attempt_id,kind,result,status,request_id)
      VALUES (p_env,v_head,p_attempt,'outcome',p_result,p_status,p_request);
    UPDATE proxy_control.environment SET highwater = v_head WHERE env = p_env;
END $$;

CREATE FUNCTION proxy_control.read_events(p_env text, p_after bigint, p_through bigint, p_limit integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, proxy_control AS $$
DECLARE v_head bigint; v_events jsonb; v_count integer; v_expected integer;
BEGIN
    PERFORM proxy_control.binding(p_env);
    IF p_after IS NULL OR p_after < 0 OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
       OR (p_through IS NOT NULL AND p_through < p_after) THEN RAISE EXCEPTION 'control rejected'; END IF;
    -- Share lock fixes a committed head and prevents writers advancing it while
    -- this bounded page is read. Counters roll back with their event insertion.
    SELECT highwater INTO v_head FROM proxy_control.environment WHERE env = p_env FOR SHARE;
    IF NOT FOUND OR p_after > v_head OR (p_through IS NOT NULL AND p_through > v_head) THEN
        RAISE EXCEPTION 'control rejected';
    END IF;
    v_head := coalesce(p_through, v_head);
    SELECT coalesce(jsonb_agg(jsonb_strip_nulls(to_jsonb(page)) ORDER BY page.seq), '[]'::jsonb)
      INTO v_events FROM (SELECT seq, attempt_id, kind, at, target_user_id, quota, result, status, request_id
        FROM proxy_control.event WHERE env = p_env AND seq > p_after AND seq <= v_head
        ORDER BY seq LIMIT p_limit) page;
    v_expected := least(p_limit::bigint, v_head - p_after)::integer;
    v_count := jsonb_array_length(v_events);
    IF v_count <> v_expected OR EXISTS (
       SELECT FROM jsonb_array_elements(v_events) WITH ORDINALITY e(value, n)
       WHERE (value->>'seq')::bigint <> p_after + n) THEN
        RAISE EXCEPTION 'control rejected';
    END IF;
    RETURN jsonb_build_object('env', p_env, 'highwater', v_head, 'events', v_events);
END $$;

CREATE FUNCTION proxy_control.ping(p_env text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, proxy_control AS $$
BEGIN
    PERFORM proxy_control.binding(p_env);
    RETURN true;
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA proxy_control FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA proxy_control FROM PUBLIC;
COMMIT;
