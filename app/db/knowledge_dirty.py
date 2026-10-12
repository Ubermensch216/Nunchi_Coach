"""원본 쓰기와 같은 SQLite 트랜잭션에서 파티션을 무효화한다."""
from __future__ import annotations


def invalidate_sql(partitions: str, reason: str) -> str:
    # 유효 근거가 다른 파티션에 남은 주장은 계속 현행이다. 사람의 거절은 보존한다.
    return f"""        INSERT INTO kg_jobs(partition_key,reason) SELECT partition_key,'{reason}'
        FROM ({partitions}) WHERE partition_key IS NOT NULL
        ON CONFLICT(partition_key,job_kind) DO UPDATE SET
        wanted_revision=kg_jobs.wanted_revision+1,attempts=0,error='',reason=excluded.reason,
        state=CASE WHEN kg_jobs.state='running' THEN 'running' ELSE 'dirty' END;
        UPDATE kg_claim_evidence SET active=0 WHERE generation_id IN
          (SELECT id FROM kg_generations WHERE active=1 AND partition_key IN ({partitions}));
        UPDATE kg_generations SET active=0,status='obsolete' WHERE active=1
          AND partition_key IN ({partitions});
        UPDATE kg_claims SET lifecycle='stale' WHERE lifecycle='active'
          AND uuid IN (SELECT s0.claim_id FROM kg_claim_evidence s0 JOIN kg_generations g0 ON g0.id=s0.generation_id
                       WHERE g0.partition_key IN ({partitions}))
          AND NOT EXISTS
          (SELECT 1 FROM kg_claim_evidence s WHERE s.claim_id=kg_claims.uuid AND s.active=1
           AND s.support_role='supports' AND s.validation_result IN ('structural','quote_matched','semantic_supported'));
        UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='kg_revision';
    """


def decision_partitions(kind, key, related="NULL"):
    entities = f"({key},{related})"
    return f"""SELECT DISTINCT g.partition_key FROM kg_generations g
        JOIN kg_claim_evidence s ON s.generation_id=g.id JOIN kg_claims c ON c.uuid=s.claim_id
        WHERE ({kind}='claim' AND c.claim_key={key})
           OR ({kind} IN ('entity','identity','alias') AND (c.subject_id IN {entities} OR c.object_id IN {entities}))
           OR ({kind}='evidence' AND s.evidence_id={key})
        UNION SELECT 'task:'||source_key FROM kg_bindings
          WHERE source_table='tasks' AND tombstone=0 AND entity_id IN {entities}
        UNION SELECT 'document:'||e.doc_id FROM kg_evidence e JOIN kg_bindings b ON b.entity_id IN {entities}
          WHERE b.source_table='document_versions' AND b.source_key=e.doc_uuid||':'||e.doc_hash AND e.doc_id IS NOT NULL"""


def install_dirty_triggers(con):
    existing = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    sources = {"documents": "'document:'||{row}.id", "tasks": "'task:'||{row}.id"}
    for table in ("task_docs", "task_items", "work_steps", "task_steps", "work_cases", "action_runs"):
        sources[table] = "coalesce('task:'||{row}.task_id,'project')"
    for table in ("document_sections", "document_index", "chunks", "doc_roles", "doc_years", "evidence"):
        sources[table] = "coalesce('document:'||{row}.doc_id,'project')"
    for table in ("artifacts", "checklist_items"):
        sources[table] = "coalesce('task:'||(SELECT task_id FROM work_cases WHERE id={row}.case_id),'project')"
    sources["action_steps"] = "coalesce('task:'||(SELECT task_id FROM action_runs WHERE id={row}.run_id),'project')"
    sources["step_runs"] = "coalesce('task:'||(SELECT task_id FROM work_steps WHERE id={row}.step_id),'project')"
    if 'notebook_sources' in existing:
        sources['notebook_sources'] = "coalesce('document:'||{row}.doc_id,'project')"

    def document_tasks(doc):
        parts = [f"SELECT partition_key FROM kg_partition_documents WHERE doc_id={doc}"]
        for table, column in (("task_docs", "doc_id"), ("task_steps", "doc_id"), ("work_cases", "basis_doc_id")):
            if table in existing:
                parts.append(f"SELECT 'task:'||task_id FROM {table} WHERE {column}={doc}")
        if 'evidence' in existing:
            for owner, table in (('item', 'task_items'), ('step', 'work_steps')):
                if table in existing:
                    parts.append(f"SELECT 'task:'||t.task_id FROM evidence e JOIN {table} t ON t.id=e.owner_id "
                                 f"WHERE e.doc_id={doc} AND e.owner_kind='{owner}'")
        return parts

    for table, expression in sources.items():
        if table not in existing:
            continue
        for operation, refs in (("INSERT", ("new",)), ("UPDATE", ("old", "new")), ("DELETE", ("old",))):
            partitions = [f"SELECT {expression.format(row=ref)} AS partition_key" for ref in refs]
            for ref in refs:
                if table == "documents" or "document:" in expression:
                    doc = f"{ref}.id" if table == "documents" else f"{ref}.doc_id"
                    partitions.extend(document_tasks(doc))
                if table == "evidence":
                    for owner, target in (("item", "task_items"), ("step", "work_steps")):
                        if target in existing:
                            partitions.append(f"SELECT 'task:'||task_id FROM {target} WHERE id={ref}.owner_id AND {ref}.owner_kind='{owner}'")
                if table == "tasks":
                    partitions.append(f"SELECT 'task:'||{ref}.merged_into WHERE {ref}.merged_into IS NOT NULL")
                if table in ("artifacts", "checklist_items") and "action_runs" in existing:
                    partitions.append(f"SELECT 'task:'||task_id FROM action_runs WHERE id={ref}.run_id")
                    partitions.append(f"SELECT 'task:'||c.task_id FROM action_runs r JOIN work_cases c ON c.id=r.case_id WHERE r.id={ref}.run_id")
                if table == 'action_runs':
                    partitions.append(f"SELECT 'task:'||task_id FROM work_cases WHERE id={ref}.case_id")
                if table == 'action_steps':
                    partitions.append(f"SELECT 'task:'||c.task_id FROM action_runs r JOIN work_cases c ON c.id=r.case_id WHERE r.id={ref}.run_id")
            when = ""
            if operation == "UPDATE":
                columns = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
                ignored = {"last_seen", "first_seen", "updated_at", "created_at", "extracted_at"}
                if table == "documents":
                    ignored |= {"analysis_status", "parse_error", "parse_note"}
                if table == 'notebook_sources':
                    ignored |= {'selected', 'allowed', 'version_hash', 'added_at'}
                when = " WHEN " + " OR ".join(f'old."{c}" IS NOT new."{c}"' for c in columns if c not in ignored)
            name = f"kg_dirty_{table}_{operation.lower()}"
            con.execute(f"DROP TRIGGER IF EXISTS {name}")
            # BEFORE DELETE에는 FK SET NULL / CASCADE 이전의 의존성과 UUID가 남아 있다.
            timing = "BEFORE" if operation == "DELETE" else "AFTER"
            extra = ""
            if operation == "DELETE":
                if table == "documents":
                    extra = "DELETE FROM meta WHERE key='kg_document_uuid:'||old.id;"
                if table in ("tasks", "work_cases", "work_steps", "task_steps", "action_steps", "artifacts"):
                    extra += f"""UPDATE kg_entities SET lifecycle='stale' WHERE uuid IN
                      (SELECT entity_id FROM kg_bindings WHERE source_table='{table}' AND source_key=CAST(old.id AS TEXT));
                      UPDATE kg_bindings SET tombstone=1,source_key=source_key||':deleted:'||entity_id
                      WHERE source_table='{table}' AND source_key=CAST(old.id AS TEXT);"""
                if table == 'task_items':
                    condition = "source_table='task_item_values' AND substr(source_key,1,instr(source_key,':')-1)=CAST(old.id AS TEXT) AND tombstone=0"
                    extra += f"""UPDATE kg_entities SET lifecycle='stale' WHERE uuid IN
                      (SELECT entity_id FROM kg_bindings WHERE {condition});
                      UPDATE kg_bindings SET tombstone=1,source_key=source_key||':deleted:'||entity_id WHERE {condition};"""
            con.execute(f"CREATE TRIGGER {name} {timing} {operation} ON {table}{when} BEGIN "
                        + invalidate_sql(" UNION ".join(partitions), f"{table}:{operation.lower()}") + extra + " END")

    con.execute("DROP TRIGGER IF EXISTS kg_dirty_meta")
    con.execute("""CREATE TRIGGER kg_dirty_meta AFTER UPDATE OF value ON meta
        WHEN new.key='index_revision' AND new.value IS NOT old.value BEGIN """
        + invalidate_sql("SELECT partition_key FROM kg_jobs WHERE job_kind='projection'", "index_revision") + " END")
    for operation, ref in (('INSERT', 'new'), ('DELETE', 'old')):
        con.execute(f"CREATE TRIGGER IF NOT EXISTS kg_dirty_meta_{operation.lower()} AFTER {operation} ON meta "
                    f"WHEN {ref}.key='index_revision' BEGIN "
                    + invalidate_sql("SELECT partition_key FROM kg_jobs WHERE job_kind='projection'", "index_revision") + " END")
    decision_keys = decision_partitions('new.target_kind', 'new.target_key', "json_extract(new.after_json,'$.entity_id')")
    con.execute("DROP TRIGGER IF EXISTS kg_dirty_decision")
    con.execute(f"""CREATE TRIGGER kg_dirty_decision AFTER INSERT ON kg_decisions BEGIN
        INSERT INTO kg_jobs(partition_key,reason) SELECT partition_key,'decision' FROM ({decision_keys})
        WHERE true ON CONFLICT(partition_key,job_kind) DO UPDATE SET
          wanted_revision=kg_jobs.wanted_revision+1,attempts=0,error='',reason='decision',
          state=CASE WHEN kg_jobs.state='running' THEN 'running' ELSE 'dirty' END;
        UPDATE kg_claims SET lifecycle='retracted' WHERE new.target_kind='claim' AND new.decision='reject'
          AND claim_key=new.target_key;
        UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='kg_revision';
        END""")
    for operation, ref in (('INSERT', 'new'), ('UPDATE', 'new'), ('DELETE', 'old')):
        human = "old.origin='human' OR new.origin='human'" if operation == 'UPDATE' else f"{ref}.origin='human'"
        alias_keys = decision_partitions("'alias'", f'{ref}.entity_id')
        if operation == 'UPDATE':
            alias_keys += ' UNION ' + decision_partitions("'alias'", 'old.entity_id')
        con.execute(f"DROP TRIGGER IF EXISTS kg_dirty_alias_{operation.lower()}")
        con.execute(f"""CREATE TRIGGER kg_dirty_alias_{operation.lower()} AFTER {operation} ON kg_aliases
            WHEN {human} BEGIN
            INSERT INTO meta(key,value) VALUES ('kg_alias_revision','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1;
            INSERT INTO kg_jobs(partition_key,reason) SELECT partition_key,'alias' FROM ({alias_keys})
            WHERE true ON CONFLICT(partition_key,job_kind) DO UPDATE SET
              wanted_revision=kg_jobs.wanted_revision+1,attempts=0,error='',reason='alias',
              state=CASE WHEN kg_jobs.state='running' THEN 'running' ELSE 'dirty' END;
            UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='kg_revision';
            END""")
