"""A reproducible 2,000-case baseline and 1,000-case untouched test cohort."""
from collections import Counter, defaultdict
import json

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

from .bulk_dataset import SEEDED_ID, corpus
from .dataset import content_hash, stable_id
from .engine import RULES
from .repository import Conflict, now
from .seeded_history import catalog, PROVENANCE

METHOD = 'seeded-baseline-3000-v1'


def balanced(rows, size):
    """One balancing rule per case; overlapping alert counts are reported separately."""
    buckets = defaultdict(list)
    for row in rows: buckets[row['balance_rule']].append(row)
    identities, strata, selected = Counter(), Counter(), []
    hashes = {r['id']: content_hash([METHOD, r['id']]) for r in rows}
    while len(selected) < size:
        available = False
        for rule in sorted(RULES):
            if not buckets[rule] or len(selected) == size: continue
            available = True
            def key(row):
                stratum = (rule, row['customer_type'], row['reference_typology'], row['reference_verdict'])
                return strata[stratum], identities[row['identity_id']], hashes[row['id']]
            row = min(buckets[rule], key=key)
            buckets[rule].remove(row); selected.append(row)
            identities[row['identity_id']] += 1
            strata[(rule, row['customer_type'], row['reference_typology'], row['reference_verdict'])] += 1
        if not available: raise ValueError('Insufficient cases for requested baseline')
    return selected


def selection():
    rows = list(catalog().values())
    identities = sorted({r['identity_id'] for r in rows}, key=lambda value: content_hash([METHOD, value]))
    training_ids = set(identities[:len(identities) * 2 // 3])
    training = balanced([r for r in rows if r['identity_id'] in training_ids], 2000)
    reserved = balanced([r for r in rows if r['identity_id'] not in training_ids], 1000)
    return training, reserved


def describe(rows):
    return {'cases': len(rows), 'identities': len({r['identity_id'] for r in rows}),
        'identity_types': dict(Counter(r['customer_type'] for r in rows)),
        'reference_verdicts': dict(Counter(r['reference_verdict'] for r in rows)),
        'typologies': dict(Counter(r['reference_typology'] for r in rows)),
        'rules': [{'rule_id': rule, 'balancing_cases': sum(r['balance_rule'] == rule for r in rows),
            'alerts': sum(rule in r['rules'] for r in rows)} for rule in RULES]}


def prepare(repo):
    if repo.settings.mode != 'local': raise Conflict('Baseline preparation is local-only')
    training, reserved = selection()
    existing = {r['case_id']: r for r in repo.list('seeded_case_history', SEEDED_ID)}
    chunks = []
    for scope, rows in [('BASELINE', training), ('RESERVED', reserved)]:
        records = []
        for row in rows:
            source = existing.get(row['id'])
            if not source or content_hash(source['body']) != source['content_hash']:
                raise Conflict('Missing or damaged seeded evidence')
            records.append({'case_id': row['id'], 'history_id': source['id'], 'history_hash': source['content_hash'],
                'identity_id': row['identity_id'], 'balance_rule': row['balance_rule']})
        for at in range(0, len(records), 400):
            body = {'method': METHOD, 'scope': scope, 'cases': records[at:at + 400]}
            ident = content_hash(body)
            old = repo.get('seeded_baseline_chunks', ident)
            if old and (old['body'] != body or old['content_hash'] != ident): raise Conflict('Baseline chunk integrity failed')
            if not old:
                try: repo.create('seeded_baseline_chunks', ident, {'dataset_id': SEEDED_ID, 'body': body, 'content_hash': ident})
                except AlreadyExists: pass
            chunks.append({'id': ident, 'scope': scope, 'count': len(body['cases'])})
    body = {'method': METHOD, 'source_manifest_hash': corpus().manifest['manifest_hash'],
        'provenance': PROVENANCE, 'training': describe(training), 'reserved': describe(reserved), 'chunks': chunks,
        'total_cases': 3000, 'human_reviews': 0, 'model_calls_created_by_preparation': 0,
        'note': 'Synthetic reference labels; actual Gemini reviews are tracked separately. Rule alerts overlap. Rare-rule limits prevent equal counts.'}
    ident = content_hash(body)
    previous = repo.get('seeded_baselines', ident)
    if previous:
        if previous['body'] != body or previous['content_hash'] != ident: raise Conflict('Baseline integrity failed')
    else: repo.create('seeded_baselines', ident, {'id': ident, 'dataset_id': SEEDED_ID, 'body': body, 'content_hash': ident, 'created_at': now()})
    # This pointer selects practice data only; it never changes an operational policy.
    repo.db.collection('seeded_baseline_state').document(SEEDED_ID).set({'dataset_id': SEEDED_ID, 'baseline_id': ident})
    repo.audit('SEEDED_BASELINE_SELECTED', 'seeded_baselines', ident, 'operator-demo', cases=3000, reserved=1000)
    return {'baseline_id': ident, **body}


def active(repo):
    pointer = repo.get('seeded_baseline_state', SEEDED_ID)
    if not pointer: return None
    record = repo.get('seeded_baselines', pointer['baseline_id'])
    if not record or content_hash(record['body']) != record['content_hash'] or record['content_hash'] != pointer['baseline_id']:
        raise Conflict('Baseline integrity failed')
    rows = {'BASELINE': [], 'RESERVED': []}
    for ref in record['body']['chunks']:
        chunk = repo.get('seeded_baseline_chunks', ref['id'])
        if not chunk or content_hash(chunk['body']) != ref['id'] or chunk['content_hash'] != ref['id']:
            raise Conflict('Baseline chunk integrity failed')
        if chunk['body']['scope'] != ref['scope'] or len(chunk['body']['cases']) != ref['count']:
            raise Conflict('Baseline chunk scope mismatch')
        rows[ref['scope']].extend(chunk['body']['cases'])
    expected_training = record['body']['training']['cases']
    expected_reserved = record['body']['reserved']['cases']
    if (len(rows['BASELINE']) != expected_training or len(rows['RESERVED']) != expected_reserved
        or expected_training + expected_reserved != record['body']['total_cases']):
        raise Conflict('Baseline counts mismatch')
    if {r['identity_id'] for r in rows['BASELINE']} & {r['identity_id'] for r in rows['RESERVED']}:
        raise Conflict('Baseline identity leakage')
    return {'id': record['id'], 'body': record['body'], **rows}


def progress(repo):
    selected = active(repo)
    if not selected: return None
    query = repo.db.collection('seeded_advisory_reviews').where(filter=firestore.FieldFilter('baseline_id', '==', selected['id']))
    reviewed = query.count().get(timeout=15)[0][0].value
    drafted = query.where(filter=firestore.FieldFilter('provenance', '==', 'LOCAL_AGENT_DRAFT_ADVISORY_NOT_HUMAN')).count().get(timeout=15)[0][0].value
    training_count, reserved_count = selected['body']['training']['cases'], selected['body']['reserved']['cases']
    return {'id': selected['id'], 'total_cases': training_count + reserved_count, 'baseline_cases': training_count, 'reserved_cases': reserved_count,
        'ai_reviewed_cases': reviewed - drafted, 'agent_draft_reviewed_cases': drafted, 'pending_ai_reviews': training_count - reviewed,
        'training': selected['body']['training'], 'reserved': selected['body']['reserved'], 'human_reviews': 0}


def run_reviews(repo, *, resume_refused=False):
    from .quick_review import inputs, run
    if repo.settings.mode != 'local' or not repo.settings.local_gemini_valid():
        raise Conflict('Verified local Gemini Free Tier access is required')
    selected = active(repo)
    if not selected: raise Conflict('Prepare the baseline before model execution')
    done = {r['case_id'] for r in repo.list('seeded_advisory_reviews', SEEDED_ID) if r['baseline_id'] == selected['id']}
    rows = [catalog()[r['case_id']] for r in selected['BASELINE'] if r['case_id'] not in done]
    processed, batch_size = 0, 5
    resume_id = 'explicit-operator-resume' if resume_refused else None
    while rows:
        daily = repo.daily_control()
        if daily['attempts_reserved'] >= daily['limit']: return {'processed': processed, 'stop': 'DAILY_CAP'}
        if not repo.settings.local_gemini_valid(): return {'processed': processed, 'stop': 'VERIFICATION_EXPIRED'}
        cases = inputs(min(batch_size, len(rows)), rows)
        result = run(repo, cases=cases, baseline_id=selected['id'], resume_id=resume_id)
        print(json.dumps({key: result.get(key) for key in ['run_id', 'state', 'error_code', 'cases', 'reused']}), flush=True)
        if result.get('error_code') == 'CONTEXT_LIMIT' and len(cases) > 1:
            batch_size = max(1, len(cases) // 2)
            continue
        if result['state'] != 'COMPLETE' or result.get('reused'):
            return {'processed': processed, 'stop': result.get('error_code') or 'PREVIOUS_ATTEMPT_REQUIRES_REVIEW'}
        completed = {c['case_id'] for c in cases}
        processed += len(completed); rows = [r for r in rows if r['id'] not in completed]
    return {'processed': processed, 'stop': 'COMPLETE'}


if __name__ == '__main__':
    import argparse
    from .config import Settings
    from .repository import Repository
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'run', 'status'], default='status', nargs='?')
    parser.add_argument('--resume-refused', action='store_true', help='Explicitly retry a sealed HTTP 429 refusal once; never retry unknown outcomes')
    args = parser.parse_args(); repo = Repository(Settings.load(), SEEDED_ID)
    result = prepare(repo) if args.action == 'prepare' else run_reviews(repo, resume_refused=args.resume_refused) if args.action == 'run' else progress(repo)
    print(json.dumps(result, default=str))
