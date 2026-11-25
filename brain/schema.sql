-- The brain as a verified knowledge graph. Applied by `brain init`; every statement is idempotent.
--
-- The DATABASE enforces:
--   * an append-only episode log;
--   * immutable fact statements, kinds, authors, entities and valid_from (supersede instead);
--   * valid_to and expired_at written once, so a superseded fact cannot be revived;
--   * a changed evidence REFERENCE voids an earlier verification;
--   * no verification of a disputed or judge-pending fact;
--   * server-set recorded_at, version and default expiry;
--   * a verifier who is not the author (the author string is self-asserted, so this is an audit
--     rule, not access control);
--   * every entity has a section;
--   * entity names and aliases resolve exactly: name_lc is unique per type, aliases are
--     lower-cased and unique per type.
-- Role names and entity types are per-project configuration, so the client checks those.
-- Every session writes with the service_role key, which bypasses RLS, so the rules are triggers
-- and CHECKs, not permissions. Anon is denied on every table and every function.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        CREATE ROLE anon NOLOGIN NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE ROLE service_role NOLOGIN NOINHERIT BYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticator') THEN
        CREATE ROLE authenticator LOGIN NOINHERIT;
    END IF;
END $$;
GRANT anon, service_role TO authenticator;
CREATE SCHEMA IF NOT EXISTS extensions;
GRANT USAGE ON SCHEMA extensions TO anon, service_role;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;
GRANT USAGE ON SCHEMA public TO anon, service_role;

CREATE TABLE IF NOT EXISTS public.brain_sections (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug text NOT NULL UNIQUE,
    title text NOT NULL
);

CREATE TABLE IF NOT EXISTS public.brain_episodes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('handoff_line','operator_order','report','command_output',
                                       'commit','doc','transcript_excerpt','handover','note')),
    body text NOT NULL CHECK (length(body) <= 1800),
    parent_id uuid REFERENCES public.brain_episodes(id),
    part_no int NOT NULL DEFAULT 1 CHECK (part_no >= 1),
    author text NOT NULL CHECK (length(btrim(author)) >= 1),
    session_id text,
    -- NULL means the time is UNKNOWN, never "now".
    occurred_at timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    source_ref text,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED
);
CREATE INDEX IF NOT EXISTS brain_episodes_fts ON public.brain_episodes USING gin (search_vector);
CREATE INDEX IF NOT EXISTS brain_episodes_recorded ON public.brain_episodes (recorded_at DESC);

CREATE OR REPLACE FUNCTION public.brain_episodes_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    RAISE EXCEPTION 'brain_episodes is append-only';
END $$;
DROP TRIGGER IF EXISTS brain_episodes_append_only ON public.brain_episodes;
CREATE TRIGGER brain_episodes_append_only BEFORE UPDATE OR DELETE ON public.brain_episodes
    FOR EACH ROW EXECUTE FUNCTION public.brain_episodes_append_only();

CREATE TABLE IF NOT EXISTS public.brain_entities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    type text NOT NULL CHECK (type ~ '^[a-z][a-z0-9_]{0,39}$'),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 300),
    name_lc text GENERATED ALWAYS AS (lower(name)) STORED,
    aliases text[] NOT NULL DEFAULT '{}',
    summary text CHECK (summary IS NULL OR length(summary) <= 400),
    -- NOT NULL: boots select facts through their entities' sections, so an unsectioned entity's
    -- facts would never reach a boot or a verify queue.
    section_id uuid NOT NULL REFERENCES public.brain_sections(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS brain_entities_type_name ON public.brain_entities (type, name_lc);
CREATE INDEX IF NOT EXISTS brain_entities_aliases ON public.brain_entities USING gin (aliases);
CREATE INDEX IF NOT EXISTS brain_entities_section ON public.brain_entities (section_id);

-- Aliases are stored lower-cased and must not name another entity of the same type (by name or
-- alias). Generated columns are computed after BEFORE triggers, so this reads lower(NEW.name).
-- Two concurrent inserts can both pass the alias check; the unique name index still holds, and
-- alias collisions are rare enough to repair by hand.
CREATE OR REPLACE FUNCTION public.brain_entities_before() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE clash uuid;
BEGIN
    NEW.aliases := ARRAY(SELECT DISTINCT lower(btrim(a)) FROM unnest(NEW.aliases) AS a
                          WHERE btrim(a) <> '' AND lower(btrim(a)) <> lower(NEW.name));
    SELECT e.id INTO clash FROM public.brain_entities e
     WHERE e.type = NEW.type AND e.id <> NEW.id
       AND (lower(e.name) = ANY(NEW.aliases) OR e.aliases && (NEW.aliases || lower(NEW.name)))
     LIMIT 1;
    IF clash IS NOT NULL THEN
        RAISE EXCEPTION 'alias_conflict: % % collides with entity %', NEW.type, NEW.name, clash;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        NEW.updated_at := now();
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS brain_entities_before ON public.brain_entities;
CREATE TRIGGER brain_entities_before BEFORE INSERT OR UPDATE ON public.brain_entities
    FOR EACH ROW EXECUTE FUNCTION public.brain_entities_before();

CREATE TABLE IF NOT EXISTS public.brain_facts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('rule','trap','decision','settled','finding','measurement',
                                       'task','observation')),
    statement text NOT NULL CHECK (length(statement) BETWEEN 8 AND 600),
    detail text CHECK (detail IS NULL OR length(detail) <= 4096),
    entity_ids uuid[] NOT NULL CHECK (cardinality(entity_ids) >= 1),
    polarity smallint NOT NULL DEFAULT 0 CHECK (polarity IN (-1, 0, 1)),
    status text NOT NULL DEFAULT 'unverified' CHECK (status IN ('unverified','verified','verified_partial',
                                       'disputed','superseded','retracted','refuted','evidence_broken')),
    judge_pending boolean NOT NULL DEFAULT false,
    valid_from timestamptz NOT NULL DEFAULT now(),
    valid_to timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    expired_at timestamptz,
    verify_by timestamptz NOT NULL,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence) = 'array'),
    author text NOT NULL CHECK (length(btrim(author)) >= 1),
    author_session text,
    verified_by text,
    verified_at timestamptz,
    verify_note text,
    episode_ids uuid[] NOT NULL DEFAULT '{}',
    version int NOT NULL DEFAULT 1,
    embedding extensions.vector(384),
    search_vector tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', statement), 'A')
        || setweight(to_tsvector('english', coalesce(detail, '')), 'B')) STORED,
    CHECK (verified_by IS NULL OR verified_by <> author),
    CHECK (status NOT IN ('verified','verified_partial','refuted') OR verified_by IS NOT NULL),
    CHECK (valid_to IS NULL OR valid_to >= valid_from),
    CHECK (kind = 'observation' OR jsonb_array_length(evidence) >= 1),
    CHECK (kind <> 'observation' OR cardinality(episode_ids) >= 1)
);
CREATE INDEX IF NOT EXISTS brain_facts_entities ON public.brain_facts USING gin (entity_ids);
CREATE INDEX IF NOT EXISTS brain_facts_fts ON public.brain_facts USING gin (search_vector);
CREATE INDEX IF NOT EXISTS brain_facts_evidence ON public.brain_facts USING gin (evidence jsonb_path_ops);
CREATE INDEX IF NOT EXISTS brain_facts_live ON public.brain_facts (status, verify_by) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS brain_facts_live_recent ON public.brain_facts (recorded_at DESC)
    WHERE valid_to IS NULL;

-- The identity of an evidence list: its (type, ref) pairs, without the check annotations the gate
-- writes. Changing a reference voids a verification; re-annotating does not.
CREATE OR REPLACE FUNCTION public.brain_evidence_refs(ev jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path = '' AS $$
    SELECT coalesce(jsonb_agg(jsonb_build_object('type', e->>'type', 'ref', e->>'ref')
                              ORDER BY e->>'type', e->>'ref'), '[]'::jsonb)
      FROM jsonb_array_elements(coalesce(ev, '[]'::jsonb)) AS e
$$;

CREATE OR REPLACE FUNCTION public.brain_facts_before() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.recorded_at := now();
        NEW.version := 1;
        IF NEW.verify_by IS NULL THEN
            NEW.verify_by := CASE NEW.kind
                WHEN 'measurement' THEN now() + interval '7 days'
                WHEN 'settled' THEN now() + interval '60 days'
                WHEN 'task' THEN now() + interval '3 days'
                WHEN 'observation' THEN 'infinity'::timestamptz
                ELSE now() + interval '30 days' END;
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.statement, NEW.kind, NEW.author, NEW.recorded_at, NEW.entity_ids, NEW.valid_from,
        NEW.polarity, NEW.author_session)
       IS DISTINCT FROM
       (OLD.statement, OLD.kind, OLD.author, OLD.recorded_at, OLD.entity_ids, OLD.valid_from,
        OLD.polarity, OLD.author_session) THEN
        RAISE EXCEPTION 'brain_facts: statement, kind, author, entities, valid_from and recorded_at are immutable; supersede instead';
    END IF;
    IF (OLD.valid_to IS NOT NULL AND NEW.valid_to IS DISTINCT FROM OLD.valid_to)
       OR (OLD.expired_at IS NOT NULL AND NEW.expired_at IS DISTINCT FROM OLD.expired_at) THEN
        RAISE EXCEPTION 'brain_facts: valid_to is write-once (a closed fact cannot be revived)';
    END IF;
    IF public.brain_evidence_refs(NEW.evidence) IS DISTINCT FROM public.brain_evidence_refs(OLD.evidence) THEN
        -- new evidence has not been checked by anyone else yet, and it reopens a refuted fact
        IF NEW.status IN ('verified', 'verified_partial', 'refuted') THEN
            NEW.status := 'unverified';
        END IF;
        NEW.verified_by := NULL;
        NEW.verified_at := NULL;
    END IF;
    IF NEW.status IN ('verified', 'verified_partial', 'refuted') AND NEW.status IS DISTINCT FROM OLD.status THEN
        IF OLD.status = 'disputed' THEN
            RAISE EXCEPTION 'brain_facts: resolve the dispute first (supersede, or judge the pair)';
        END IF;
        IF NEW.judge_pending THEN
            RAISE EXCEPTION 'brain_facts: judge_pending candidates must be judged before verification';
        END IF;
    END IF;
    -- An embedding-only update is not a new version of the fact.
    IF (to_jsonb(NEW) - 'embedding') IS DISTINCT FROM (to_jsonb(OLD) - 'embedding') THEN
        NEW.version := OLD.version + 1;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS brain_facts_before ON public.brain_facts;
CREATE TRIGGER brain_facts_before BEFORE INSERT OR UPDATE ON public.brain_facts
    FOR EACH ROW EXECUTE FUNCTION public.brain_facts_before();

CREATE OR REPLACE FUNCTION public.brain_facts_no_delete() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    RAISE EXCEPTION 'brain_facts rows are never deleted; retract or supersede';
END $$;
DROP TRIGGER IF EXISTS brain_facts_no_delete ON public.brain_facts;
CREATE TRIGGER brain_facts_no_delete BEFORE DELETE ON public.brain_facts
    FOR EACH ROW EXECUTE FUNCTION public.brain_facts_no_delete();

CREATE TABLE IF NOT EXISTS public.brain_links (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    from_id uuid NOT NULL,
    from_kind text NOT NULL CHECK (from_kind IN ('fact','entity','episode')),
    to_id uuid NOT NULL,
    to_kind text NOT NULL CHECK (to_kind IN ('fact','entity','episode')),
    rel text NOT NULL CHECK (rel IN ('supports','contradicts','supersedes','depends_on',
                                     'derived_from','same_as','part_of','candidate')),
    author text NOT NULL CHECK (length(btrim(author)) >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (from_id, to_id, rel)
);
CREATE INDEX IF NOT EXISTS brain_links_to ON public.brain_links (to_id);

-- Append-only pair decisions. Historical decisions survive closed facts.
CREATE TABLE IF NOT EXISTS public.brain_judgements (
    pair_lo uuid NOT NULL REFERENCES public.brain_facts(id),
    pair_hi uuid NOT NULL REFERENCES public.brain_facts(id),
    verdict text NOT NULL CHECK (verdict IN ('duplicate','refines','supersedes','contradicts','unrelated')),
    author text NOT NULL CHECK (length(btrim(author)) >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (pair_lo, pair_hi), CHECK (pair_lo < pair_hi)
);

-- Anon is denied on every table. Only service_role reads and writes, because it bypasses RLS. A
-- process holding only an anon key would read an EMPTY brain rather than an error, so the client
-- refuses to run without the service key.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['brain_sections','brain_episodes','brain_entities','brain_facts',
                             'brain_links','brain_judgements'] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC, anon', t);
    END LOOP;
END $$;
-- Least privilege for the one writer role: no DELETE anywhere.
GRANT SELECT, INSERT, UPDATE ON public.brain_sections, public.brain_entities, public.brain_facts TO service_role;
GRANT SELECT, INSERT ON public.brain_episodes, public.brain_links, public.brain_judgements TO service_role;

-- One stable client UUID owns the fact and all judged changes; a lost reply is safe to replay.
CREATE OR REPLACE FUNCTION public.brain_write_fact(p_new jsonb, p_actions jsonb)
RETURNS SETOF public.brain_facts
LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    new_id uuid := (p_new->>'id')::uuid;
    new_row public.brain_facts;
    old_row public.brain_facts;
    action jsonb;
    relation text;
    starts timestamptz := coalesce((p_new->>'valid_from')::timestamptz, now());
BEGIN
    IF new_id IS NULL OR jsonb_typeof(p_actions) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'brain_write_fact: id and action array required';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(new_id::text, 0));
    SELECT * INTO new_row FROM public.brain_facts WHERE id = new_id;
    IF FOUND THEN
        IF (new_row.kind, new_row.statement, new_row.author, new_row.entity_ids)
           IS DISTINCT FROM (p_new->>'kind', p_new->>'statement', p_new->>'author',
               ARRAY(SELECT jsonb_array_elements_text(p_new->'entity_ids'))::uuid[]) THEN
            RAISE EXCEPTION 'brain_write_fact: operation_identity_conflict';
        END IF;
        RETURN NEXT new_row;
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM jsonb_array_elements(p_actions) a
               WHERE coalesce(a->>'verdict', '') NOT IN
                   ('supersedes','contradicts','refines','unrelated','candidate')
                  OR (a->>'id')::uuid IS NULL OR (a->>'id')::uuid = new_id)
       OR (SELECT count(*) <> count(DISTINCT a->>'id') FROM jsonb_array_elements(p_actions) a) THEN
        RAISE EXCEPTION 'brain_write_fact: invalid or duplicate action';
    END IF;
    -- Lock in the same order across writers. A stale judgement retries in the client; it never
    -- silently overwrites a newer lifecycle state. All changes roll back on a conflict.
    FOR action IN SELECT a FROM jsonb_array_elements(p_actions) a
                  WHERE a->>'verdict' IN ('supersedes','contradicts') ORDER BY a->>'id' LOOP
        SELECT * INTO old_row FROM public.brain_facts WHERE id = (action->>'id')::uuid FOR UPDATE;
        IF NOT FOUND OR old_row.version IS DISTINCT FROM (action->>'version')::int
           OR old_row.valid_to IS NOT NULL
           OR old_row.status NOT IN ('unverified','verified','verified_partial','disputed','evidence_broken') THEN
            RAISE EXCEPTION 'version_conflict';
        END IF;
        IF action->>'verdict' = 'supersedes' AND starts < old_row.valid_from THEN
            RAISE EXCEPTION 'supersede_backdated';
        END IF;
    END LOOP;
    INSERT INTO public.brain_facts (id, kind, statement, detail, entity_ids, polarity, evidence,
                                    author, author_session, episode_ids, valid_from, verify_by,
                                    judge_pending, status)
    VALUES (new_id, p_new->>'kind', p_new->>'statement', p_new->>'detail',
            ARRAY(SELECT jsonb_array_elements_text(p_new->'entity_ids'))::uuid[],
            coalesce((p_new->>'polarity')::smallint, 0), coalesce(p_new->'evidence', '[]'::jsonb),
            p_new->>'author', p_new->>'author_session',
            ARRAY(SELECT jsonb_array_elements_text(coalesce(p_new->'episode_ids', '[]'::jsonb)))::uuid[],
            starts, (p_new->>'verify_by')::timestamptz,
            EXISTS (SELECT 1 FROM jsonb_array_elements(p_actions) a WHERE a->>'verdict' = 'candidate'),
            CASE WHEN EXISTS (SELECT 1 FROM jsonb_array_elements(p_actions) a WHERE a->>'verdict' = 'contradicts')
                 THEN 'disputed' ELSE 'unverified' END)
    RETURNING * INTO new_row;
    FOR action IN SELECT a FROM jsonb_array_elements(p_actions) a LOOP
        relation := CASE action->>'verdict' WHEN 'refines' THEN 'derived_from'
                    WHEN 'unrelated' THEN NULL ELSE action->>'verdict' END;
        IF action->>'verdict' = 'supersedes' THEN
            UPDATE public.brain_facts SET status = 'superseded', valid_to = starts, expired_at = now()
             WHERE id = (action->>'id')::uuid;
        ELSIF action->>'verdict' = 'contradicts' THEN
            UPDATE public.brain_facts SET status = 'disputed' WHERE id = (action->>'id')::uuid;
        END IF;
        IF action->>'verdict' <> 'candidate' THEN
            INSERT INTO public.brain_judgements (pair_lo, pair_hi, verdict, author)
            VALUES (least(new_id, (action->>'id')::uuid), greatest(new_id, (action->>'id')::uuid),
                    action->>'verdict', new_row.author)
            ON CONFLICT (pair_lo, pair_hi) DO NOTHING;
        END IF;
        IF relation IS NOT NULL THEN
            INSERT INTO public.brain_links (from_id, from_kind, to_id, to_kind, rel, author)
            VALUES (new_id, 'fact', (action->>'id')::uuid, 'fact', relation, new_row.author);
        END IF;
    END LOOP;
    RETURN NEXT new_row;
END $$;

-- Resolve one candidate pair atomically. Re-sending the same verdict is a replay; a different
-- verdict for a judged pair is refused. `refines` only adds a derived_from link, so a LIVE newer
-- fact may refine a CLOSED older one; every other verdict against a closed fact is refused.
CREATE OR REPLACE FUNCTION public.brain_judge_pair(p_newer uuid, p_older uuid, p_newer_version int,
    p_older_version int, p_verdict text, p_author text)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE n public.brain_facts; o public.brain_facts; prior text; relation text; n_closed boolean; o_closed boolean;
BEGIN
    IF p_newer = p_older THEN RAISE EXCEPTION 'same_fact'; END IF;
    IF p_author IS NULL OR btrim(p_author) = '' THEN RAISE EXCEPTION 'invalid_judge'; END IF;
    IF p_verdict NOT IN ('duplicate','refines','supersedes','contradicts','unrelated') OR p_verdict IS NULL THEN
        RAISE EXCEPTION 'invalid_verdict';
    END IF;
    PERFORM id FROM public.brain_facts WHERE id IN (p_newer, p_older) ORDER BY id FOR UPDATE;
    SELECT * INTO STRICT n FROM public.brain_facts WHERE id = p_newer;
    SELECT * INTO STRICT o FROM public.brain_facts WHERE id = p_older;
    relation := CASE p_verdict WHEN 'duplicate' THEN 'same_as' WHEN 'refines' THEN 'derived_from'
                WHEN 'supersedes' THEN 'supersedes' WHEN 'contradicts' THEN 'contradicts' END;
    SELECT verdict INTO prior FROM public.brain_judgements
     WHERE pair_lo = least(p_newer, p_older) AND pair_hi = greatest(p_newer, p_older);
    IF FOUND THEN
        IF prior <> p_verdict THEN RAISE EXCEPTION 'judgement_conflict'; END IF;
        IF p_verdict IN ('duplicate','refines','supersedes') AND NOT EXISTS (
            SELECT 1 FROM public.brain_links WHERE from_id = p_newer AND to_id = p_older AND rel = relation) THEN
            RAISE EXCEPTION 'judgement_orientation_conflict';
        END IF;
        RETURN jsonb_build_object('verdict', prior, 'replayed', true);
    END IF;
    IF n.version <> p_newer_version OR o.version <> p_older_version
       OR p_newer_version IS NULL OR p_older_version IS NULL THEN
        RAISE EXCEPTION 'version_conflict';
    END IF;
    n_closed := n.valid_to IS NOT NULL OR n.status NOT IN ('unverified','verified','verified_partial','disputed','evidence_broken');
    o_closed := o.valid_to IS NOT NULL OR o.status NOT IN ('unverified','verified','verified_partial','disputed','evidence_broken');
    IF p_verdict <> 'unrelated' AND (n_closed OR (o_closed AND p_verdict <> 'refines')) THEN
        RAISE EXCEPTION 'closed_fact';
    END IF;
    IF p_verdict = 'duplicate' THEN
        UPDATE public.brain_facts SET status = 'retracted', valid_to = now(), expired_at = now() WHERE id = p_newer;
    ELSIF p_verdict = 'supersedes' THEN
        IF n.valid_from < o.valid_from THEN RAISE EXCEPTION 'backdated_supersession'; END IF;
        UPDATE public.brain_facts SET status = 'superseded', valid_to = n.valid_from, expired_at = now() WHERE id = p_older;
    ELSIF p_verdict = 'contradicts' THEN
        UPDATE public.brain_facts SET status = 'disputed' WHERE id IN (p_newer, p_older);
    END IF;
    IF relation IS NOT NULL THEN
        INSERT INTO public.brain_links (from_id, from_kind, to_id, to_kind, rel, author)
        VALUES (p_newer, 'fact', p_older, 'fact', relation, p_author) ON CONFLICT DO NOTHING;
    END IF;
    INSERT INTO public.brain_judgements (pair_lo, pair_hi, verdict, author)
    VALUES (least(p_newer, p_older), greatest(p_newer, p_older), p_verdict, p_author);
    UPDATE public.brain_facts f SET judge_pending = false
     WHERE f.id IN (p_newer, p_older) AND f.judge_pending AND NOT EXISTS (
        SELECT 1 FROM public.brain_links l
         WHERE l.rel = 'candidate' AND l.from_kind = 'fact' AND l.to_kind = 'fact'
           AND (l.from_id = f.id OR l.to_id = f.id)
           AND NOT EXISTS (SELECT 1 FROM public.brain_judgements j
                            WHERE j.pair_lo = least(l.from_id, l.to_id) AND j.pair_hi = greatest(l.from_id, l.to_id)));
    RETURN jsonb_build_object('verdict', p_verdict, 'replayed', false);
END $$;

-- Facts on the entities of some sections, joined in the database: the entity list of a section
-- can hold thousands of ids and does not fit a URL. The result is cut at p_limit with NO
-- truncation signal: a caller that needs completeness compares its row count with
-- brain_queue_count (or the limit) and prints the gap.
CREATE OR REPLACE FUNCTION public.brain_facts_in_sections(p_section_ids uuid[], p_kinds text[] DEFAULT NULL,
    p_queue_only boolean DEFAULT false, p_exclude_author text DEFAULT NULL,
    p_oldest_first boolean DEFAULT false, p_limit int DEFAULT 500)
RETURNS SETOF public.brain_facts
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
    SELECT f.* FROM public.brain_facts f
     WHERE f.valid_to IS NULL
       AND f.status IN ('unverified','verified','verified_partial','disputed','evidence_broken')
       AND f.kind <> 'observation'
       AND EXISTS (SELECT 1 FROM unnest(f.entity_ids) AS u(id)
                     JOIN public.brain_entities e ON e.id = u.id
                    WHERE e.section_id = ANY(p_section_ids))
       AND (p_kinds IS NULL OR f.kind = ANY(p_kinds))
       AND (NOT p_queue_only OR f.status = 'unverified' OR f.judge_pending)
       AND (p_exclude_author IS NULL OR f.author <> p_exclude_author)
     ORDER BY CASE WHEN p_oldest_first THEN f.recorded_at END ASC,
              CASE WHEN NOT p_oldest_first THEN f.recorded_at END DESC
     LIMIT greatest(1, p_limit);
$$;

CREATE OR REPLACE FUNCTION public.brain_queue_count(p_section_ids uuid[], p_exclude_author text DEFAULT NULL)
RETURNS bigint
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
    SELECT count(*) FROM public.brain_facts f
     WHERE f.valid_to IS NULL AND f.kind <> 'observation'
       AND f.status IN ('unverified','verified','verified_partial','disputed','evidence_broken')
       AND (f.status = 'unverified' OR f.judge_pending)
       AND (p_exclude_author IS NULL OR f.author <> p_exclude_author)
       AND EXISTS (SELECT 1 FROM unnest(f.entity_ids) AS u(id)
                     JOIN public.brain_entities e ON e.id = u.id
                    WHERE e.section_id = ANY(p_section_ids));
$$;

-- Every fact on some entities, oldest first. No row cap: the contradiction candidate stage must
-- see the OLD facts a new one may reverse, and a newest-first cap drops exactly those.
CREATE OR REPLACE FUNCTION public.brain_facts_for_entities(p_entity_ids uuid[], p_kinds text[] DEFAULT NULL,
    p_live_only boolean DEFAULT true)
RETURNS SETOF public.brain_facts
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
    SELECT f.* FROM public.brain_facts f
     WHERE f.entity_ids && p_entity_ids
       AND (p_kinds IS NULL OR f.kind = ANY(p_kinds))
       AND (NOT p_live_only OR (f.valid_to IS NULL AND
            f.status IN ('unverified','verified','verified_partial','disputed','evidence_broken')))
     ORDER BY f.recorded_at ASC;
$$;

-- Ranked full-text search over live facts. p_any = false: every query word must match (web-search
-- syntax). p_any = true: any word may match, but at least two distinct words must (one, when the
-- query has only one), so a long prompt does not pull in facts that share a single common word.
CREATE OR REPLACE FUNCTION public.brain_search_facts(p_query text, p_any boolean DEFAULT false, p_limit int DEFAULT 20)
RETURNS SETOF public.brain_facts
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
    WITH q AS (
        SELECT CASE WHEN p_any
                    THEN nullif(replace(plainto_tsquery('english', p_query)::text, '&', '|'), '')::tsquery
                    ELSE websearch_to_tsquery('english', p_query) END AS tsq,
               tsvector_to_array(to_tsvector('english', p_query)) AS words)
    SELECT f.* FROM public.brain_facts f, q
     WHERE f.search_vector @@ q.tsq AND f.valid_to IS NULL
       AND f.status IN ('unverified','verified','verified_partial','disputed','evidence_broken')
       AND (NOT p_any OR (SELECT count(*) FROM unnest(tsvector_to_array(f.search_vector)) AS w
                           WHERE w = ANY(q.words)) >= least(2, cardinality(q.words)))
     ORDER BY ts_rank(f.search_vector, q.tsq) DESC, f.recorded_at DESC
     LIMIT greatest(1, p_limit);
$$;

CREATE OR REPLACE FUNCTION public.brain_match_facts(query_embedding text, match_count int DEFAULT 20)
RETURNS TABLE (id uuid, similarity real)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
    SELECT f.id, (1 - (f.embedding OPERATOR(extensions.<=>) query_embedding::extensions.vector))::real
      FROM public.brain_facts f
     WHERE f.embedding IS NOT NULL AND f.valid_to IS NULL
       AND f.status IN ('unverified','verified','verified_partial','disputed','evidence_broken')
     ORDER BY f.embedding OPERATOR(extensions.<=>) query_embedding::extensions.vector
     LIMIT greatest(1, match_count);
$$;

-- Bounded missing-only embedding writes. No fact or status authority.
CREATE OR REPLACE FUNCTION public.brain_set_embeddings(p_rows jsonb)
RETURNS integer LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE r jsonb; n integer; written integer := 0;
BEGIN
    IF p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' OR jsonb_array_length(p_rows) > 64 THEN
        RAISE EXCEPTION 'brain_set_embeddings: expected an array of at most 64 rows';
    END IF;
    FOR r IN SELECT value FROM jsonb_array_elements(p_rows) ORDER BY value->>'id' LOOP
        UPDATE public.brain_facts SET embedding = (r->>'embedding')::extensions.vector
         WHERE id = (r->>'id')::uuid AND embedding IS NULL;
        GET DIAGNOSTICS n = ROW_COUNT;
        written := written + n;
    END LOOP;
    RETURN written;
END $$;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO service_role;

NOTIFY pgrst, 'reload schema';
