import json
from collections import defaultdict

from analyze_performance import _is_win, _load_rows

MIN_SAMPLES = 5


def _weight_from_win_rate(win_rate: float) -> float:
    """
    Convert historical win rate to a multiplier.
    50% baseline, capped to reduce overfitting.
    """
    raw = 0.6 + (win_rate * 0.8)  # 0.6 at 0%, 1.0 at 50%, 1.4 at 100%
    return max(0.7, min(1.3, raw))


def generate_catalyst_feedback(output_path: str = 'catalyst_feedback.json') -> dict:
    """
    Analyze historical outcomes and generate dynamic catalyst multipliers.
    For now, symbol-level multipliers are produced for direct use in ai_catalyst.py.
    """
    trades, scans = _load_rows()

    if not trades:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump({}, f, indent=2, sort_keys=True)
        return {}

    by_symbol = defaultdict(lambda: {'wins': 0, 'total': 0})
    by_grade = defaultdict(lambda: {'wins': 0, 'total': 0})

    for trade in trades:
        symbol = (trade['symbol'] or '').upper()
        outcome = (trade['outcome'] or '').lower()
        scan = scans.get(int(trade['scan_id']) or 0, {})
        grade = ((scan or {}).get('best_pick') or {}).get('setup_grade', 'UNKNOWN')
        is_win = _is_win(outcome)

        if symbol:
            by_symbol[symbol]['total'] += 1
            by_symbol[symbol]['wins'] += int(is_win)

        by_grade[grade]['total'] += 1
        by_grade[grade]['wins'] += int(is_win)

    feedback = {}
    for symbol, stats in by_symbol.items():
        if stats['total'] < MIN_SAMPLES:
            continue
        win_rate = stats['wins'] / stats['total']
        feedback[symbol] = round(_weight_from_win_rate(win_rate), 3)

    # Keep grade-level stats for transparency/future use.
    feedback['_meta'] = {
        'min_samples': MIN_SAMPLES,
        'grade_win_rates': {
            grade: round(stats['wins'] / stats['total'], 4)
            for grade, stats in by_grade.items()
            if stats['total'] > 0
        },
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(feedback, f, indent=2, sort_keys=True)

    return feedback


if __name__ == '__main__':
    results = generate_catalyst_feedback()
    learned_symbols = max(0, len(results) - (1 if '_meta' in results else 0))
    print(f'Wrote catalyst feedback for {learned_symbols} symbol(s).')
