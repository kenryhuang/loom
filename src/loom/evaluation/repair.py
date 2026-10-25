"""One bounded retry policy for graph and memory, independent of error wording.

Format failures are repaired before evaluating missing coverage. A mixed batch
of format and semantic failures charges both budgets. Exhausted categories never
fall through to another budget. Explicit resume starts a new cycle; the history
of previous cycles remains auditable on the unit.
"""

LIMITS = {"format": 2, "semantic": 1}


def begin_cycle(unit):
    if unit.get("repair_state"):
        unit.setdefault("repair_history", []).append(unit["repair_state"])
    unit["repair_state"] = dict(counts={k: 0 for k in LIMITS}, attempts=0, decisions=[])
    unit.update(repair=0, format_repair=0)  # compatibility for saved consumers


def retry(unit, issues):
    if not issues:
        return False
    state = unit["repair_state"]
    categories = {e["category"] for e in issues}
    active = categories - {"coverage"}
    if not active:
        active = {"semantic"}
    allowed = active <= LIMITS.keys() and all(state["counts"][k] < LIMITS[k] for k in active)
    decision = dict(codes=sorted({e["code"] for e in issues}), categories=sorted(active), retry=allowed)
    state["decisions"].append(decision)
    if allowed:
        state["attempts"] += 1
        for key in active:
            state["counts"][key] += 1
        unit.update(repair=state["counts"]["semantic"], format_repair=state["counts"]["format"])
    return allowed


def reserve_repair(cp, unit, issues, settings, *, remaining_work=0):
    """Persistent input-level limits, plus a shared 20% repair ceiling."""
    from loom.evaluation.goal_loops.facts import identity
    from loom.evaluation.rules import policy

    if unit.get('repair_reserved'):
        return True
    actionable = [e for e in issues if e.get('category') != 'coverage'] or issues
    strategies = {policy(e)['repair_strategy'] for e in actionable} - {'none'}
    if not strategies or not strategies <= {'format', 'candidate'}:
        return False
    digest = identity(unit.get('manifest', {}).get('id'), unit.get('cards'), unit.get('protocol_version'),
                      unit.get('dependency_digest'), unit.get('retry_generation', 0))
    ledger = unit.setdefault('repair_ledger', {})
    counts = ledger.setdefault(digest, {})
    spent = cp.setdefault('repair_usage', {'calls': 0})
    allowed = (all(counts.get(s, 0) < 1 for s in strategies)
               and spent['calls'] < settings['max_calls'] // 5
               and settings['max_calls'] - cp['usage']['calls'] > remaining_work)
    unit.setdefault('repair_decisions', []).append(dict(input_digest=digest, strategies=sorted(strategies), allowed=allowed))
    if allowed:
        for strategy in strategies:
            counts[strategy] = counts.get(strategy, 0) + 1
        spent['calls'] += 1
        unit['repair_reserved'] = True
    return allowed


def provider_retry(cp, failure, settings):
    """Two consecutive transient failures pause the shared evaluator queue."""
    cp['provider_failures'] = cp.get('provider_failures', 0) + 1
    return (cp['provider_failures'] < 2 and cp['usage']['calls'] < settings['max_calls']
            and bool(failure.issues) and all(e.get('retryable', False) for e in failure.issues))
