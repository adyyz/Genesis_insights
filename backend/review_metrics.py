"""Operational analyst-review counts, never reference-label accuracy."""
from collections import Counter, defaultdict


def review_summary(cases, customers, alerts, feedback, steps):
    cases = {row['id']: row for row in cases}
    customers = {row['id']: row for row in customers}
    alerts = {row['id']: row for row in alerts}
    latest = {}
    for event in feedback:
        if event.get('provenance') != 'HUMAN_OPERATOR' or event.get('alert_id') not in alerts:
            continue
        previous = latest.get(event['alert_id'])
        if previous is None or event['revision'] > previous['revision']:
            latest[event['alert_id']] = event
    classifications = {row['run_id']: row['output'] for row in steps
        if row.get('leg_id') == 'A3' and row.get('state') == 'SUCCEEDED'
        and row.get('execution_provenance') == 'REAL_PROVIDER' and row.get('output')
        and row.get('validation', {}).get('schema_pass') and row.get('validation', {}).get('evidence_pass')}
    labels = {row['run_id']: row['output'].get('predicted_verdict') for row in steps
        if row.get('leg_id') == 'A0' and row.get('state') == 'SUCCEEDED' and row.get('output')
        and row.get('execution_provenance') == 'DETERMINISTIC'
        and row.get('validation', {}).get('schema_pass') and row.get('validation', {}).get('evidence_pass')}
    totals = Counter()
    groups = {key: defaultdict(Counter) for key in ('typology', 'secondary_typology', 'rule', 'identity_type', 'risk_bucket', 'signal')}
    evidence_by_run = {row['run_id']: row['output'] for row in steps if row.get('leg_id') == 'A1'
        and row.get('state') == 'SUCCEEDED' and row.get('execution_provenance') == 'DETERMINISTIC' and row.get('output')}
    reviewed_cases = set()
    for alert_id, event in latest.items():
        alert = alerts[alert_id]
        case = cases.get(alert['case_id'])
        if not case or event.get('case_id') != case['id']: continue
        verdict = event.get('verdict')
        if verdict not in {'TRUE_POSITIVE', 'FALSE_POSITIVE', 'INCONCLUSIVE'}: continue
        output = classifications.get(event.get('triage_run_id'), {})
        suggestion = {'AUTO_TP': 'TRUE_POSITIVE', 'AUTO_FP': 'FALSE_POSITIVE', 'AUTO_UNCERTAIN': 'INCONCLUSIVE'}.get(event.get('machine_label', labels.get(event.get('triage_run_id'))))
        comparable = verdict != 'INCONCLUSIVE' and suggestion in {'TRUE_POSITIVE', 'FALSE_POSITIVE'}
        values = Counter(reviewed_alerts=1)
        values[verdict.lower()] = 1
        values['compared'] = int(comparable)
        values['agreed'] = int(comparable and verdict == suggestion)
        values['suggestion_unavailable'] = int(suggestion is None)
        values['machine_uncertain'] = int(suggestion == 'INCONCLUSIVE')
        totals.update(values)
        reviewed_cases.add(case['id'])
        evidence = evidence_by_run.get(event.get('triage_run_id'))
        from .engine import risk_assessment
        risk = risk_assessment(evidence) if evidence else {'tier': 'UNAVAILABLE'}
        for dimension, name in (
            ('typology', output.get('primary_typology', 'NOT_RECORDED')),
            ('rule', alert['rule_id']),
            ('identity_type', customers.get(case['customer_id'], {}).get('customer_type', 'NOT_RECORDED')),
            ('risk_bucket', risk['tier'])):
            groups[dimension][name].update(values)
        for item in output.get('secondary_typologies', []):
            groups['secondary_typology'][item['typology']].update(values)
        for signal in set((evidence or {}).get('signals', [])):
            groups['signal'][signal].update(values)
    def counts(counter):
        keys = ('reviewed_alerts', 'true_positive', 'false_positive', 'inconclusive', 'compared', 'agreed', 'suggestion_unavailable', 'machine_uncertain')
        return {**{key: counter[key] for key in keys},
            'agreement_rate': counter['agreed'] / counter['compared'] if counter['compared'] else None}
    return {'total_cases': len(cases), 'demo_cases': sum(row.get('split') == 'DEMO' for row in cases.values()),
        'total_identities': len(customers), 'total_alerts': len(alerts), 'reviewed_cases': len(reviewed_cases),
        **counts(totals), 'groups': {dimension: [{'name': name, **counts(counter)}
            for name, counter in sorted(rows.items())] for dimension, rows in groups.items()},
        'provenance': 'HUMAN_OPERATOR_REVIEWS_AND_VALIDATED_MODEL_SUGGESTIONS',
        'population_accuracy': None, 'population_fpr': None,
        'note': 'Agreement with analyst verdicts on reviewed alerts only. Unreviewed cases are unknown; this is not population accuracy or a held-out evaluation.'}
