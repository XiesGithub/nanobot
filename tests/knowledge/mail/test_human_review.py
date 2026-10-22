import hashlib
import json

import pytest

from tests.knowledge.mail.prepare_human_review import REPORTS
from tests.knowledge.mail.score_human_review import score


def fixture(tmp_path):
    review = {'reviewer': None, 'reviewed_at': None, 'reports': {}, 'cases': []}
    for name in REPORTS:
        data = {'cases': [{'question': 'fictional question',
            'claim_reviews': [{'claim': {'text': 'fictional claim'}}]}]}
        path = tmp_path / name
        path.write_text(json.dumps(data), encoding='utf-8')
        review['reports'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        review['cases'].append({'case_id': f'{name}:1', 'question': 'fictional question',
            'case_supported': None, 'claims': [{'claim_number': 1,
                'text': 'fictional claim', 'human_supported': None}]})
    path = tmp_path / 'synthetic-verdicts.json'
    return review, path


def test_pending_is_not_a_human_pass_and_only_supplied_booleans_are_scored(tmp_path):
    review, path = fixture(tmp_path)
    path.write_text(json.dumps(review))
    result = score(tmp_path, path)
    assert result['human_claim_support_rate'] is None and not result['human_gate_passed']
    # Synthetic labels validate the scoring code only, never the real reports.
    review.update(reviewer='synthetic-test-identity', reviewed_at='synthetic-date')
    for case in review['cases']:
        case['case_supported'] = True
        case['claims'][0]['human_supported'] = True
    path.write_text(json.dumps(review))
    assert score(tmp_path, path)['human_gate_passed']
    review['cases'][0]['case_supported'] = False
    path.write_text(json.dumps(review))
    assert not score(tmp_path, path)['human_gate_passed']


@pytest.mark.parametrize('change', ['hash', 'missing', 'text', 'label'])
def test_review_cannot_silently_drop_or_replace_pinned_evidence(tmp_path, change):
    review, path = fixture(tmp_path)
    if change == 'hash':
        review['reports'][REPORTS[0]] = 'invalid'
    elif change == 'missing':
        review['cases'].pop()
    elif change == 'text':
        review['cases'][0]['claims'][0]['text'] = 'changed claim'
    else:
        review['cases'][0]['case_supported'] = 'true'
    path.write_text(json.dumps(review))
    with pytest.raises(ValueError):
        score(tmp_path, path)
