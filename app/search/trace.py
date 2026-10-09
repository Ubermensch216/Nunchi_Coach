"""본문을 기록하지 않는 검색 진단. DB generation JSON에 함께 보존한다."""
from contextlib import contextmanager
from time import perf_counter


class RetrievalTrace:
    def __init__(self):
        self.started = perf_counter()
        self.timing = {}
        self.counts = {}
        self.path = 'rag'

    @contextmanager
    def stage(self, name):
        started = perf_counter()
        try:
            yield
        finally:
            self.timing[name + '_ms'] = round((perf_counter() - started) * 1000, 3)

    def record(self, **counts):
        self.counts.update(counts)

    def finish(self, answer):
        record = {'version': 1, 'path': self.path, **self.counts,
                  'timing': {**self.timing, 'total_ms': round((perf_counter() - self.started) * 1000, 3)}}
        if answer.generation.get('mode') == 'search_only':
            record['path'] = 'search_only'
        record['reason'] = answer.generation.get('withheld') or (
            'runtime_error' if answer.error else 'no_evidence' if answer.withheld else 'answered')
        answer.generation['retrieval'] = record
        return answer
