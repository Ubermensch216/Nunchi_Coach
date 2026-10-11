"""사람 결정과 정본 업무 합치기를 끝점 변경 없이 읽기 뷰에 적용한다."""
import json


def effective_decisions(rows):
    cancelled, result = set(), []
    for row in reversed(rows):
        if row['uuid'] in cancelled:
            continue
        if row['decision'] == 'undo':
            cancelled.add(row['supersedes_decision'])
        else:
            result.append(row)
    return list(reversed(result))


def latest_decision(rows):
    return next(iter(reversed(effective_decisions(rows))), None)


def canonical_entity(db, entity_id):
    """동명은 합치지 않는다. 명시 판정/merged_into만 따라가며 원본 UUID를 보존한다."""
    current, seen = entity_id, set()
    while current not in seen:
        seen.add(current)
        rows = db.con.execute("SELECT * FROM kg_decisions WHERE target_kind='identity' AND target_key=? ORDER BY rowid", (current,)).fetchall()
        decision = latest_decision(rows)
        if decision and decision['decision'] == 'separate':
            return current
        target = json.loads(decision['after_json']).get('entity_id') if decision and decision['decision'] == 'merge' else None
        if target is None:
            row = db.con.execute("SELECT b2.entity_id FROM kg_bindings b JOIN tasks t ON CAST(t.id AS TEXT)=b.source_key "
                "JOIN kg_bindings b2 ON b2.source_table='tasks' AND b2.source_key=CAST(t.merged_into AS TEXT) AND b2.tombstone=0 "
                "WHERE b.source_table='tasks' AND b.entity_id=? AND b.tombstone=0", (current,)).fetchone()
            target = row[0] if row else None
        if not target:
            return current
        kinds = db.con.execute('SELECT uuid,type FROM kg_entities WHERE uuid IN (?,?)', (current, target)).fetchall()
        if len(kinds) != 2 or len({r['type'] for r in kinds}) != 1:
            raise ValueError('동일성 끝점 유형이 다릅니다.')
        current = target
    raise ValueError('동일성 관계가 순환합니다.')
