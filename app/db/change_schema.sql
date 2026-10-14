-- 분석마다 업무 값이 어떻게 바뀌었나 (규제개혁 개선계획서 P2: 새 문서의 영향 추적)
--
-- 새 공문이 들어와 다시 분석하면 기한·제출처가 바뀔 수 있다. 화면은 지금 값만 보여 주므로
-- 무엇이 바뀌었는지 알 수 없었다. 다시 복원할 때마다 자동으로 읽은 값의 전후를 남긴다.
-- 사람이 고친 값은 여기 남기지 않는다(그 이력은 audit_logs에 있다).
CREATE TABLE IF NOT EXISTS restore_changes (
    id        INTEGER PRIMARY KEY,
    task_id   INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run       TEXT NOT NULL,                 -- 그 복원의 시각(같은 복원의 변경을 묶는다)
    change    TEXT NOT NULL,                 -- added | changed | removed
    kind      TEXT NOT NULL,
    slot      TEXT NOT NULL,
    before    TEXT NOT NULL DEFAULT '',
    after     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS restore_changes_task ON restore_changes(task_id, run);
