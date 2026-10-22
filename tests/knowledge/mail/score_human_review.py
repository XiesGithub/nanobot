"""Validate human verdicts against pinned evidence; never generate or fill labels."""

import argparse
import hashlib
import json
from pathlib import Path

from tests.knowledge.mail.prepare_human_review import REPORTS


def score(root: Path, review_path: Path):
    review = json.loads(review_path.read_text(encoding='utf-8'))
    expected = {}
    for name in REPORTS:
        path = root / name
        if review.get('reports', {}).get(name) != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError('report_hash_mismatch')
        data = json.loads(path.read_text(encoding='utf-8'))
        for i, case in enumerate(data.get('cases', data.get('rounds', [])), 1):
            expected[f'{name}:{i}'] = (case['question'], [c['claim']['text']
                for c in case.get('claim_reviews', [])])
    cases = review['cases']
    if len(cases) != len(expected) or {c['case_id'] for c in cases} != set(expected):
        raise ValueError('case_set_mismatch')
    claims, case_labels = [], []
    for case in cases:
        question, texts = expected[case['case_id']]
        if case['question'] != question or [c['text'] for c in case['claims']] != texts:
            raise ValueError('evidence_text_mismatch')
        if [c['claim_number'] for c in case['claims']] != list(range(1, len(texts) + 1)):
            raise ValueError('claim_set_mismatch')
        case_labels.append(case.get('case_supported'))
        claims.extend(c.get('human_supported') for c in case['claims'])
    if any(v is not None and type(v) is not bool for v in claims + case_labels):
        raise ValueError('invalid_verdict')
    pending = sum(v is None for v in claims)
    pending_cases = sum(v is None for v in case_labels)
    identity = all(isinstance(review.get(k), str) and review[k].strip()
                   for k in ('reviewer', 'reviewed_at'))
    complete = identity and not pending and not pending_cases
    supported = sum(v is True for v in claims)
    rate = supported / len(claims) if complete and claims else None
    failed_cases = [c['case_id'] for c in cases if c.get('case_supported') is False]
    return {'status': 'pending' if not complete else 'reviewed',
        'case_count': len(cases), 'claim_count': len(claims),
        'pending_claims': pending, 'pending_cases': pending_cases,
        'supported_claims': supported, 'human_claim_support_rate': rate,
        'failed_cases': failed_cases,
        'human_gate_passed': complete and rate is not None and rate >= .95 and not failed_cases,
        'note': 'Computes supplied human verdicts only; does not verify reviewer identity or certify production.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('docs/mail-rag-evaluation'))
    parser.add_argument('--review', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = score(args.root, args.review)
    except (ValueError, KeyError, TypeError, OSError):
        print(json.dumps({'status': 'invalid', 'human_gate_passed': False}))
        raise SystemExit(2) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['human_gate_passed'] else 2)
