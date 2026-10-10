"""Offline evaluation of independently labelled document/task assignments.

Input JSON: {"tasks": [stable task keys], "documents": {document key: [task keys]}}.
An empty gold label list denotes non-business/reference material. Prediction keys
must be mapped to the same task taxonomy by the reviewer before evaluation.
Gold labels are read only by this tool, never by product discovery code.
Output goes to stdout so no input document can be overwritten.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _validated(payload: dict) -> tuple[set[str], dict[str, set[str]]]:
    tasks, documents = payload.get('tasks'), payload.get('documents')
    if not isinstance(tasks, list) or not all(isinstance(x, str) and x for x in tasks):
        raise ValueError('tasks must contain nonempty stable task keys')
    if len(set(tasks)) != len(tasks) or not isinstance(documents, dict):
        raise ValueError('duplicate tasks or invalid documents')
    taxonomy = set(tasks)
    labels = {}
    for key, values in documents.items():
        if not isinstance(key, str) or not key or not isinstance(values, list):
            raise ValueError('invalid document labels')
        if not all(isinstance(x, str) and x in taxonomy for x in values):
            raise ValueError(f'unknown task key in {key}')
        if len(set(values)) != len(values):
            raise ValueError(f'duplicate label in {key}')
        labels[key] = set(values)
    return taxonomy, labels


def evaluate(gold: dict, predicted: dict) -> dict:
    taxonomy, truth = _validated(gold)
    predicted_tasks, guesses = _validated(predicted)
    if taxonomy != predicted_tasks:
        raise ValueError('map predicted tasks to the gold taxonomy before evaluation')
    if guesses.keys() - truth.keys():
        raise ValueError('prediction contains documents outside the labelled evaluation set')
    expected = {(doc, task) for doc, labels in truth.items() for task in labels}
    actual = {(doc, task) for doc, labels in guesses.items() for task in labels}
    tp, fp, fn = len(expected & actual), len(actual - expected), len(expected - actual)
    # Undefined metrics stay null: an empty test set must not look like 100%.
    ratio = lambda n, d: n / d if d else None
    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    per_task = {}
    for task in sorted(taxonomy):
        good = {d for d, labels in truth.items() if task in labels}
        found = {d for d, labels in guesses.items() if task in labels}
        per_task[task] = {'gold_documents': len(good), 'predicted_documents': len(found),
                          'precision': ratio(len(good & found), len(found)),
                          'recall': ratio(len(good & found), len(good))}
    relevant = {d for d, labels in truth.items() if labels}
    irrelevant = set(truth) - relevant
    multi = {d for d, labels in truth.items() if len(labels) > 1}
    represented = {task for doc, task in expected & actual}
    supported = {task for _, task in expected}
    return {'metric_unit': 'document_task_assignment', 'documents': len(truth),
            'missing_prediction_rows': sorted(truth.keys() - guesses.keys()),
            'true_positive': tp, 'false_positive': fp, 'false_negative': fn,
            'precision': precision, 'recall': recall,
            'f1': ratio(2 * tp, 2 * tp + fp + fn),
            'exact_assignment_accuracy': ratio(sum(truth[d] == guesses.get(d, set()) for d in truth), len(truth)),
            'business_document_coverage': ratio(sum(bool(truth[d] & guesses.get(d, set())) for d in relevant), len(relevant)),
            'nonbusiness_documents': len(irrelevant),
            'nonbusiness_assigned_documents': sum(bool(guesses.get(d)) for d in irrelevant),
            'nonbusiness_assignment_rate': ratio(sum(bool(guesses.get(d)) for d in irrelevant), len(irrelevant)),
            'multitask_documents': len(multi),
            'multitask_exact_accuracy': ratio(sum(truth[d] == guesses.get(d, set()) for d in multi), len(multi)),
            'task_presence_recall': ratio(len(represented), len(supported)),
            'per_task': per_task,
            'limitations': ['task_presence_is_not_procedure_accuracy',
                            'quality_requires_independent_representative_gold_labels']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('gold', type=Path)
    parser.add_argument('predicted', type=Path)
    args = parser.parse_args(argv)
    try:
        result = evaluate(json.loads(args.gold.read_text(encoding='utf-8-sig')),
                          json.loads(args.predicted.read_text(encoding='utf-8-sig')))
    except (ValueError, OSError, AttributeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
