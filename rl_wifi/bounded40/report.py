"""Predeclared two-block decision, never treating per-second data as repeats."""


def judge(rows):
    if len(rows) != 10 or {(r['block'], r['policy']) for r in rows} != {
            (b, p) for b in (1, 2) for p in ('P', 'E', 'middle', 'rule', 'DQN')}:
        raise ValueError('exactly ten explicit test runs required')
    results = []
    for block in (1, 2):
        data = {r['policy']: r for r in rows if r['block'] == block}
        for r in data.values():
            if r['collection_valid'] is not True:
                raise ValueError('invalid collection is not a performance zero')
            if set(r['states']) != {'uniform', 'burst'}:
                raise ValueError('both business states required')
            for s in r['states'].values():
                if s['mature'] != 3000 or not 0 <= s['on_time'] <= s['mature']:
                    raise ValueError('full per-state generation denominator required')
            if r['background_Mbps'] <= 0:
                raise ValueError('background result missing')

        def feasible(row):
            return all(s['on_time']/s['mature'] >= .95 for s in row['states'].values())

        candidates = [p for p in ('P', 'E', 'middle') if feasible(data[p])]
        best = max(candidates, key=lambda p: data[p]['background_Mbps']) if candidates else None
        dqn_ok, rule_ok = feasible(data['DQN']), feasible(data['rule'])
        gain = data['DQN']['background_Mbps']/data[best]['background_Mbps']-1 if best else None
        rule_gain = data['DQN']['background_Mbps']/data['rule']['background_Mbps']-1 if rule_ok else None
        results.append({'block': block, 'dqn_feasible': dqn_ok, 'best_fixed': best,
                        'gain_over_best_fixed': gain, 'rule_feasible': rule_ok, 'gain_over_rule': rule_gain})
    if not all(r['dqn_feasible'] for r in results):
        code = 'stop_deadline_or_inconsistent'
    elif all(r['best_fixed'] is None for r in results):
        code = 'deadline_advantage_needs_independent_replication'
    elif all(r['gain_over_best_fixed'] is not None and r['gain_over_best_fixed'] >= .05-1e-12 for r in results):
        code = 'gain_signal_needs_independent_replication'
    else:
        code = 'stop_no_repeated_practical_gain'
    return {'code': code, 'blocks': results, 'runs_per_policy': 2,
            'dqn_necessity_unproven': not all(r['gain_over_rule'] is not None and r['gain_over_rule'] >= .05-1e-12 for r in results),
            'automatic_extra_runs': False, 'automatic_more_training': False}
