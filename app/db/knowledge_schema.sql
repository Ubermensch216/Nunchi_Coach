-- 그래프 파생 색인과 보존 이력. 온톨로지 어휘는 ontology.json에서 설치한다.
CREATE TABLE IF NOT EXISTS kg_entity_types (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS kg_predicates (
    name TEXT PRIMARY KEY,
    definition TEXT NOT NULL CHECK(json_valid(definition))
);
CREATE TABLE IF NOT EXISTS kg_entities (
    uuid TEXT PRIMARY KEY,
    type TEXT NOT NULL REFERENCES kg_entity_types(name),
    label TEXT NOT NULL,
    scope_key TEXT NOT NULL DEFAULT '',
    lifecycle TEXT NOT NULL DEFAULT 'active' CHECK(lifecycle IN ('active','stale','retracted')),
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS kg_bindings (
    source_table TEXT NOT NULL,
    source_key TEXT NOT NULL,
    entity_id TEXT NOT NULL REFERENCES kg_entities(uuid),
    tombstone INTEGER NOT NULL DEFAULT 0 CHECK(tombstone IN (0,1)),
    PRIMARY KEY(source_table,source_key)
);
CREATE INDEX IF NOT EXISTS idx_kg_bindings_entity ON kg_bindings(entity_id);
CREATE TABLE IF NOT EXISTS kg_generations (
    id INTEGER PRIMARY KEY,
    partition_key TEXT NOT NULL,
    wanted_revision INTEGER NOT NULL,
    input_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'building' CHECK(status IN ('building','validated','published','failed','obsolete')),
    active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1) AND (active=0 OR status='published')),
    processed INTEGER NOT NULL DEFAULT 0 CHECK(processed>=0),
    failed INTEGER NOT NULL DEFAULT 0 CHECK(failed>=0),
    versions TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(versions)),
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kg_generation_active ON kg_generations(partition_key) WHERE active=1;
CREATE TABLE IF NOT EXISTS kg_jobs (
    partition_key TEXT NOT NULL,
    job_kind TEXT NOT NULL DEFAULT 'projection',
    wanted_revision INTEGER NOT NULL DEFAULT 1 CHECK(wanted_revision>0),
    state TEXT NOT NULL DEFAULT 'dirty' CHECK(state IN ('dirty','running','done','failed','cancelled')),
    lease TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
    reason TEXT NOT NULL DEFAULT '',
    lease_until REAL,
    leased_revision INTEGER,
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(partition_key,job_kind)
);
CREATE INDEX IF NOT EXISTS idx_kg_jobs_state ON kg_jobs(state,lease);
CREATE TABLE IF NOT EXISTS kg_partition_documents (
    partition_key TEXT NOT NULL,
    doc_id INTEGER NOT NULL,
    PRIMARY KEY(partition_key,doc_id)
);
CREATE INDEX IF NOT EXISTS idx_kg_partition_doc ON kg_partition_documents(doc_id);
CREATE TABLE IF NOT EXISTS kg_claims (
    uuid TEXT PRIMARY KEY,
    claim_key TEXT NOT NULL UNIQUE,
    subject_id TEXT NOT NULL REFERENCES kg_entities(uuid),
    predicate TEXT NOT NULL REFERENCES kg_predicates(name),
    object_id TEXT REFERENCES kg_entities(uuid),
    value_json TEXT CHECK(value_json IS NULL OR json_valid(value_json)),
    value_type TEXT,
    scope_key TEXT NOT NULL DEFAULT '',
    valid_from TEXT CHECK(valid_from IS NULL OR (length(valid_from)=10 AND substr(valid_from,1,4)!='0000' AND date(valid_from,'+0 days') IS NOT NULL AND date(valid_from,'+0 days')=valid_from)),
    valid_to TEXT CHECK(valid_to IS NULL OR (length(valid_to)=10 AND substr(valid_to,1,4)!='0000' AND date(valid_to,'+0 days') IS NOT NULL AND date(valid_to,'+0 days')=valid_to)),
    temporal_reason TEXT NOT NULL DEFAULT 'unknown',
    conditions TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(conditions) AND json_type(conditions)='object'),
    origin TEXT NOT NULL CHECK(origin IN ('projection','rule','model','human')),
    basis_state TEXT NOT NULL DEFAULT 'stated' CHECK(basis_state IN ('stated','inferred','conflict','missing')),
    applicability TEXT NOT NULL DEFAULT 'unknown' CHECK(applicability IN ('current','past','unknown')),
    human_state TEXT NOT NULL DEFAULT 'none' CHECK(human_state IN ('none','predecessor','successor','recheck')),
    execution_state TEXT,
    lifecycle TEXT NOT NULL DEFAULT 'stale' CHECK(lifecycle IN ('active','stale','retracted')),
    generation_id INTEGER REFERENCES kg_generations(id),
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    retracted_at TEXT,
    CHECK((object_id IS NOT NULL AND value_json IS NULL AND value_type IS NULL)
       OR (object_id IS NULL AND value_json IS NOT NULL AND value_type IS NOT NULL AND json_type(value_json)!='null')),
    CHECK(valid_from IS NULL OR valid_to IS NULL OR valid_from<valid_to),
    CHECK(object_id IS NULL OR object_id!=subject_id)
);
CREATE INDEX IF NOT EXISTS idx_kg_claim_subject ON kg_claims(subject_id,predicate);
CREATE INDEX IF NOT EXISTS idx_kg_claim_object ON kg_claims(object_id,predicate);
CREATE INDEX IF NOT EXISTS idx_kg_claim_scope ON kg_claims(scope_key,lifecycle);
CREATE TABLE IF NOT EXISTS kg_evidence (
    uuid TEXT PRIMARY KEY,
    anchor_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('document_quote','source_record','human_statement')),
    doc_uuid TEXT,
    doc_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    doc_hash TEXT,
    parse_fingerprint TEXT,
    locator TEXT NOT NULL DEFAULT '',
    exact_quote TEXT NOT NULL DEFAULT '',
    offsets TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(offsets)),
    chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
    source_record_snapshot TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(source_record_snapshot)),
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(kind!='document_quote' OR (doc_uuid IS NOT NULL AND doc_hash IS NOT NULL AND parse_fingerprint IS NOT NULL AND length(exact_quote)>0 AND length(locator)>0))
);
CREATE INDEX IF NOT EXISTS idx_kg_evidence_doc ON kg_evidence(doc_id,doc_hash);
CREATE TABLE IF NOT EXISTS kg_decisions (
    uuid TEXT PRIMARY KEY,
    target_kind TEXT NOT NULL CHECK(target_kind IN ('entity','alias','claim','identity','evidence')),
    target_key TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('confirm','reject','merge','separate','alias','correct','undo','recheck')),
    before_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(before_json)),
    after_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(after_json)),
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    supersedes_decision TEXT REFERENCES kg_decisions(uuid)
);
CREATE INDEX IF NOT EXISTS idx_kg_decisions_target ON kg_decisions(target_kind,target_key,created_at);
CREATE TRIGGER IF NOT EXISTS kg_decision_undo BEFORE INSERT ON kg_decisions WHEN new.decision='undo' BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM kg_decisions d WHERE d.uuid=new.supersedes_decision
      AND d.target_kind=new.target_kind AND d.target_key=new.target_key)
    THEN RAISE(ABORT,'kg undo target mismatch') END;
END;
CREATE TABLE IF NOT EXISTS kg_aliases (
    entity_id TEXT NOT NULL REFERENCES kg_entities(uuid),
    label TEXT NOT NULL,
    normalized_label TEXT NOT NULL,
    alias_kind TEXT NOT NULL DEFAULT 'name',
    scope_key TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL CHECK(origin IN ('projection','rule','model','human')),
    evidence_id TEXT REFERENCES kg_evidence(uuid),
    decision_id TEXT REFERENCES kg_decisions(uuid),
    source_key TEXT NOT NULL,
    PRIMARY KEY(entity_id,normalized_label,scope_key,origin,source_key),
    CHECK(evidence_id IS NOT NULL OR decision_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_kg_alias_lookup ON kg_aliases(normalized_label,scope_key);
CREATE TABLE IF NOT EXISTS kg_claim_evidence (
    claim_id TEXT NOT NULL REFERENCES kg_claims(uuid),
    evidence_id TEXT NOT NULL REFERENCES kg_evidence(uuid),
    support_role TEXT NOT NULL DEFAULT 'supports' CHECK(support_role IN ('supports','refutes','context')),
    validation_result TEXT NOT NULL DEFAULT 'pending' CHECK(validation_result IN ('pending','structural','quote_matched','semantic_supported','invalid')),
    validation_version TEXT NOT NULL,
    generation_id INTEGER NOT NULL REFERENCES kg_generations(id),
    active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)),
    PRIMARY KEY(claim_id,evidence_id,support_role,generation_id)
);
CREATE INDEX IF NOT EXISTS idx_kg_support_evidence ON kg_claim_evidence(evidence_id,active);
CREATE INDEX IF NOT EXISTS idx_kg_support_generation ON kg_claim_evidence(generation_id,active);
CREATE VIEW IF NOT EXISTS kg_current_claims AS
    SELECT c.* FROM kg_claims c WHERE c.lifecycle='active' AND EXISTS (
        SELECT 1 FROM kg_claim_evidence s JOIN kg_generations g ON g.id=s.generation_id
        WHERE s.claim_id=c.uuid AND s.active=1 AND g.active=1 AND g.status='published'
        AND s.support_role='supports' AND s.validation_result IN ('structural','quote_matched','semantic_supported')
    );
CREATE TABLE IF NOT EXISTS kg_claim_links (
    from_claim TEXT NOT NULL REFERENCES kg_claims(uuid),
    kind TEXT NOT NULL CHECK(kind IN ('supersedes','conflicts')),
    to_claim TEXT NOT NULL REFERENCES kg_claims(uuid),
    support_ref TEXT NOT NULL REFERENCES kg_evidence(uuid),
    PRIMARY KEY(from_claim,kind,to_claim),
    CHECK(from_claim!=to_claim),
    CHECK(kind!='conflicts' OR from_claim<to_claim)
);
CREATE INDEX IF NOT EXISTS idx_kg_link_target ON kg_claim_links(to_claim,kind);
CREATE TABLE IF NOT EXISTS kg_claim_events (
    uuid TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES kg_claims(uuid),
    event_kind TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    generation_id INTEGER REFERENCES kg_generations(id),
    decision_id TEXT REFERENCES kg_decisions(uuid),
    before_json TEXT NOT NULL CHECK(json_valid(before_json)),
    after_json TEXT NOT NULL CHECK(json_valid(after_json))
);
CREATE INDEX IF NOT EXISTS idx_kg_event_claim ON kg_claim_events(claim_id,recorded_at);

CREATE TRIGGER IF NOT EXISTS kg_claim_domain BEFORE INSERT ON kg_claims BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM kg_predicates p JOIN kg_entities s ON s.uuid=new.subject_id,
        json_each(p.definition,'$.domain') d
        WHERE p.name=new.predicate AND s.type=d.value
    ) THEN RAISE(ABORT,'kg domain mismatch') END;
    SELECT CASE WHEN new.object_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM kg_predicates p JOIN kg_entities o ON o.uuid=new.object_id
        JOIN kg_entities s ON s.uuid=new.subject_id, json_each(p.definition,'$.range') r
        WHERE p.name=new.predicate AND o.type=r.value
          AND (coalesce(json_extract(p.definition,'$.same_type'),0)=0 OR s.type=o.type)
    ) THEN RAISE(ABORT,'kg range mismatch') END;
    SELECT CASE WHEN new.value_json IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM kg_predicates p WHERE p.name=new.predicate
        AND json_extract(p.definition,'$.value_type')=new.value_type
    ) THEN RAISE(ABORT,'kg literal type mismatch') END;
    SELECT CASE WHEN new.value_type='date' AND (
        json_type(new.value_json)!='text' OR length(json_extract(new.value_json,'$'))!=10
        OR substr(json_extract(new.value_json,'$'),1,4)='0000'
        OR date(json_extract(new.value_json,'$'),'+0 days') IS NULL
        OR date(json_extract(new.value_json,'$'),'+0 days')!=json_extract(new.value_json,'$')
    ) THEN RAISE(ABORT,'kg invalid date') END;
    SELECT CASE WHEN new.value_type='decimal_amount' AND (
        json_type(new.value_json)!='object' OR coalesce(json_type(new.value_json,'$.amount'),'')!='text'
        OR NOT (coalesce(json_type(new.value_json,'$.currency')='text' AND length(trim(json_extract(new.value_json,'$.currency')))>0,0)
             OR coalesce(json_type(new.value_json,'$.unit')='text' AND length(trim(json_extract(new.value_json,'$.unit')))>0,0))
        OR EXISTS(SELECT 1 FROM json_each(new.value_json) WHERE key NOT IN ('amount','currency','unit'))
        OR EXISTS(SELECT 1 FROM json_each(new.value_json) WHERE key IN ('currency','unit') AND (type!='text' OR length(trim(value))=0))
        OR json_extract(new.value_json,'$.amount') GLOB '*[^0-9.-]*'
        OR json_extract(new.value_json,'$.amount') NOT GLOB '*[0-9]*'
        OR instr(substr(json_extract(new.value_json,'$.amount'),2),'-')>0
        OR json_extract(new.value_json,'$.amount') GLOB '*.*.*'
        OR json_extract(new.value_json,'$.amount') GLOB '*.'
        OR ltrim(json_extract(new.value_json,'$.amount'),'-') GLOB '.*'
        OR (ltrim(json_extract(new.value_json,'$.amount'),'-') GLOB '0[0-9]*')
    ) THEN RAISE(ABORT,'kg invalid decimal amount') END;
    SELECT CASE WHEN new.object_id IS NOT NULL
        AND coalesce((SELECT json_extract(definition,'$.acyclic') FROM kg_predicates WHERE name=new.predicate),0)=1
        AND EXISTS (
            WITH RECURSIVE path(id) AS (
                SELECT new.object_id UNION
                SELECT c.object_id FROM kg_claims c JOIN path p ON c.subject_id=p.id
                WHERE c.predicate=new.predicate AND c.scope_key=new.scope_key AND c.lifecycle!='retracted'
                  AND ((new.generation_id IS NULL AND (c.generation_id IS NULL OR c.lifecycle='active'))
                       OR c.generation_id=new.generation_id)
            ) SELECT 1 FROM path WHERE id=new.subject_id
        ) THEN RAISE(ABORT,'kg relation cycle') END;
END;
CREATE TRIGGER IF NOT EXISTS kg_entity_type_immutable BEFORE UPDATE OF uuid,type ON kg_entities BEGIN
    SELECT RAISE(ABORT,'kg entity identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS kg_claim_immutable BEFORE UPDATE OF uuid,claim_key,subject_id,predicate,object_id,value_json,value_type,scope_key,valid_from,valid_to,conditions,origin,recorded_at ON kg_claims BEGIN
    SELECT RAISE(ABORT,'kg logical claim is immutable');
END;
CREATE TRIGGER IF NOT EXISTS kg_claim_no_delete BEFORE DELETE ON kg_claims BEGIN
    SELECT RAISE(ABORT,'kg claims retain history');
END;
CREATE TRIGGER IF NOT EXISTS kg_evidence_immutable BEFORE UPDATE OF uuid,anchor_key,kind,doc_uuid,doc_hash,parse_fingerprint,locator,exact_quote,offsets,source_record_snapshot,recorded_at ON kg_evidence BEGIN
    SELECT RAISE(ABORT,'kg evidence snapshot is immutable');
END;
CREATE TRIGGER IF NOT EXISTS kg_evidence_no_delete BEFORE DELETE ON kg_evidence BEGIN
    SELECT RAISE(ABORT,'kg evidence retains history');
END;
CREATE TRIGGER IF NOT EXISTS kg_decision_no_update BEFORE UPDATE ON kg_decisions BEGIN
    SELECT RAISE(ABORT,'kg decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS kg_decision_no_delete BEFORE DELETE ON kg_decisions BEGIN
    SELECT RAISE(ABORT,'kg decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS kg_event_no_update BEFORE UPDATE ON kg_claim_events BEGIN
    SELECT RAISE(ABORT,'kg events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS kg_event_no_delete BEFORE DELETE ON kg_claim_events BEGIN
    SELECT RAISE(ABORT,'kg events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS kg_claim_observed AFTER INSERT ON kg_claims BEGIN
    INSERT INTO kg_claim_events(uuid,claim_id,event_kind,generation_id,before_json,after_json)
    VALUES(lower(hex(randomblob(16))),new.uuid,'observed',new.generation_id,'{}',
           json_object('lifecycle',new.lifecycle,'basis_state',new.basis_state,'applicability',new.applicability,'human_state',new.human_state));
END;
CREATE TRIGGER IF NOT EXISTS kg_claim_state AFTER UPDATE OF lifecycle,basis_state,applicability,human_state,execution_state ON kg_claims
WHEN old.lifecycle!=new.lifecycle OR old.basis_state!=new.basis_state OR old.applicability!=new.applicability OR old.human_state!=new.human_state OR old.execution_state IS NOT new.execution_state BEGIN
    INSERT INTO kg_claim_events(uuid,claim_id,event_kind,generation_id,before_json,after_json)
    VALUES(lower(hex(randomblob(16))),new.uuid,'state',new.generation_id,
      json_object('lifecycle',old.lifecycle,'basis_state',old.basis_state,'applicability',old.applicability,'human_state',old.human_state,'execution_state',old.execution_state),
      json_object('lifecycle',new.lifecycle,'basis_state',new.basis_state,'applicability',new.applicability,'human_state',new.human_state,'execution_state',new.execution_state));
END;
CREATE TRIGGER IF NOT EXISTS kg_links_cycle BEFORE INSERT ON kg_claim_links
WHEN new.kind='supersedes' BEGIN
    SELECT CASE WHEN EXISTS (
        WITH RECURSIVE path(id) AS (
            SELECT new.to_claim UNION
            SELECT l.to_claim FROM kg_claim_links l JOIN path p ON l.from_claim=p.id WHERE l.kind='supersedes'
        ) SELECT 1 FROM path WHERE id=new.from_claim
    ) THEN RAISE(ABORT,'kg supersedes cycle') END;
END;
CREATE TRIGGER IF NOT EXISTS kg_links_no_update BEFORE UPDATE ON kg_claim_links BEGIN
    SELECT RAISE(ABORT,'kg claim links are immutable');
END;
-- 다른 프로젝트에서 가져온 사람 판정의 처리 기록. 적용하지 못한 판정도 지우지 않고 보존한다.
CREATE TABLE IF NOT EXISTS kg_imported_decisions (
    source_uuid TEXT PRIMARY KEY,
    source_project TEXT NOT NULL DEFAULT '',
    seq INTEGER NOT NULL,
    payload TEXT NOT NULL CHECK(json_valid(payload)),
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','applied','kept_local')),
    local_decision TEXT REFERENCES kg_decisions(uuid),
    note TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_kg_imported_state ON kg_imported_decisions(state, seq);
CREATE INDEX IF NOT EXISTS idx_kg_generation_partition ON kg_generations(partition_key);
