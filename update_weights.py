import json
from collections import defaultdict

from analyze_performance import create_report_app
from models import Trade

MIN_SAMPLES = 5


def _is_win_from_pnl(pnl: float) -> bool:
    return pnl > 0


def _weight_from_win_rate(win_rate: float) -> float:
    raw = 0.6 + (win_rate * 0.8)
    return max(0.7, min(1.3, raw))


def generate_catalyst_feedback(output_path: str = 'catalyst_feedback.json') -> dict:
    by_symbol = defaultdict(lambda: {'wins': 0, 'total': 0})

    trades = Trade.query.filter(Trade.pnl.isnot(None)).all()

    for trade in trades:
        symbol = (trade.symbol or '').upper().strip()
        if not symbol:
            continue
        pnl = float(trade.pnl or 0.0)
        is_win = _is_win_from_pnl(pnl)
        by_symbol[symbol]['total'] += 1
        by_symbol[symbol]['wins'] += int(is_win)

    feedback = {}
    for symbol, stats in by_symbol.items():
        if stats['total'] < MIN_SAMPLES:
            continue
        win_rate = stats['wins'] / stats['total']
        feedback[symbol] = round(_weight_from_win_rate(win_rate), 3)

    feedback['_meta'] = {
        'min_samples': MIN_SAMPLES,
        'symbols_seen': len(by_symbol),
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(feedback, f, indent=2, sort_keys=True)

    return feedback


if __name__ == '__main__':
    app = create_report_app()
    with app.app_context():
        results = generate_catalyst_feedback()
    learned_symbols = max(0, len(results) - (1 if '_meta' in results else 0))
    print(f'Wrote catalyst feedback for {learned_symbols} symbol(s).')
