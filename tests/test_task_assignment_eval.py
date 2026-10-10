import pytest

from app.tools.task_assignment_eval import evaluate


def test_missing_singleton_wrong_template_nonbusiness_and_multitask_are_counted():
    tasks = ['grant', 'disclosure', 'inspection']
    gold = {'tasks': tasks, 'documents': {'same-form-1': ['grant'], 'same-form-2': ['disclosure'],
            'different-form': ['grant'], 'singleton': ['inspection'],
            'two-jobs': ['grant', 'disclosure'], 'reference': []}}
    predicted = {'tasks': tasks, 'documents': {'same-form-1': ['grant'], 'same-form-2': ['grant'],
            'different-form': ['grant'], 'two-jobs': ['grant'], 'reference': ['grant']}}
    result = evaluate(gold, predicted)
    assert (result['true_positive'], result['false_positive'], result['false_negative']) == (3, 2, 3)
    assert result['precision'] == 3/5 and result['recall'] == 3/6
    assert result['missing_prediction_rows'] == ['singleton']
    assert result['business_document_coverage'] == 3/5
    assert result['nonbusiness_assignment_rate'] == 1
    assert result['multitask_exact_accuracy'] == 0
    assert result['task_presence_recall'] == 1/3


def test_perfect_multitask_assignments_use_each_pair_once():
    labels = {'tasks': ['a', 'b'], 'documents': {'one': ['a', 'b'], 'two': ['a'], 'reference': []}}
    result = evaluate(labels, labels)
    assert result['true_positive'] == 3 and result['f1'] == 1
    assert result['nonbusiness_assignment_rate'] == 0
    assert result['multitask_exact_accuracy'] == 1


def test_empty_set_does_not_claim_perfect_accuracy():
    result = evaluate({'tasks': [], 'documents': {}}, {'tasks': [], 'documents': {}})
    assert result['precision'] is None and result['exact_assignment_accuracy'] is None


@pytest.mark.parametrize('prediction', [
    {'tasks': ['a'], 'documents': {'outside': ['a']}},
    {'tasks': ['other'], 'documents': {'one': ['other']}},
    {'tasks': ['a'], 'documents': {'one': ['a', 'a']}},
])
def test_unmapped_duplicate_or_unlabelled_predictions_rejected(prediction):
    with pytest.raises(ValueError):
        evaluate({'tasks': ['a'], 'documents': {'one': ['a']}}, prediction)
