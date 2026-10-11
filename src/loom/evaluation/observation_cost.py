"""Indexed recorded interval costs without copying cumulative call-ID lists into every fact."""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime


def observation_cost_counter(facts):
    models, tools = defaultdict(list), defaultdict(list)
    ledger = {r['round_id']: r for r in facts['token_ledger']}
    for call in facts['model_calls']:
        ref = call.get('request_ref') or call.get('response_ref')
        if ref:
            models[call['episode_id']].append((ref['line_number'], ledger.get(call['id'], {}).get('total_tokens')))
    for tool in facts['tool_uses']:
        ref = tool.get('input_ref') or tool.get('raw_output_ref')
        if ref:
            ep = facts['event_episode_ids'].get(str(ref['line_number']))
            tools[ep].append(ref['line_number'])
    indexes = {}
    for ep in models.keys() | tools.keys():
        rows = sorted(models[ep], key=lambda r: r[0])
        tokens, missing = [0], [0]
        for _, value in rows:
            tokens.append(tokens[-1] + (value or 0))
            missing.append(missing[-1] + int(value is None))
        indexes[ep] = ([r[0] for r in rows], tokens, missing, sorted(tools[ep]))

    def cost(start, end, episode_id, *, exclusive_start=False):
        if start is None or end is None or end < start:
            return {'status': 'unknown', 'basis': 'Missing or reversed endpoints'}
        lines, tokens, missing, tool_lines = indexes.get(episode_id, ([], [0], [0], []))
        lower = start + int(exclusive_start)
        a, b = bisect_left(lines, lower), bisect_right(lines, end)
        unmeasured = missing[b] - missing[a]
        elapsed = None
        try:
            times = facts['event_times']
            delta = datetime.fromisoformat(times[str(end)]) - datetime.fromisoformat(times[str(start)])
            if delta.total_seconds() >= 0:
                elapsed = round(delta.total_seconds() * 1000)
        except (KeyError, TypeError, ValueError):
            pass
        return dict(status='partial' if unmeasured else 'measured', known_tokens=tokens[b] - tokens[a],
                    unmeasured_model_calls=unmeasured, model_calls=b-a,
                    tool_calls=bisect_right(tool_lines, end) - bisect_left(tool_lines, lower), elapsed_ms=elapsed,
                    elapsed_basis='recorded_wall_span_including_waits' if elapsed is not None else 'missing_timestamps',
                    start_line=start, end_line=end, episode_id=episode_id)
    return cost
