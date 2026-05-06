import argparse
from datetime import date, timedelta
from app import app, db
from models import BlogPublishingPlan

def norm(t):
    return ' '.join((t or '').lower().split())

def next_monday(start):
    days = (7 - start.weekday()) % 7
    return start if days == 0 else start + timedelta(days=days)

TOPICS = [
("What Is a Trading Playbook? A Beginner-Friendly Guide","trading playbook beginner","informational","top","guide",1),
("Paper Trading Before Live Trading: Why Testing Rules Matters","paper trading before live trading","informational","top","guide",1),
("Bracket Orders Explained: Stops, Targets, and Risk Controls","bracket orders explained","informational","middle","educational",1),
("ORB Trading Basics: Opening Range Breakouts Without the Hype","orb trading basics","informational","middle","educational",2),
("VWAP in Day Trading: How Traders Use It for Context","vwap day trading context","informational","middle","educational",2),
("Why Automated Trading Still Needs Human Risk Rules","automated trading risk rules","informational","middle","article",2),
("How to Build a Repeatable Day Trading Routine","repeatable day trading routine","informational","top","guide",2),
("Trading Discipline vs. Trading Emotion: Why Rules Matter","trading discipline vs emotion","informational","top","article",2),
("Broker API Connections Explained for Retail Traders","broker api connections retail traders","informational","middle","educational",3),
("What Makes a Stock ‘In Play’ for Day Traders?","stock in play day traders","informational","top","educational",3),
("Risk Per Trade Explained: Why Position Sizing Matters","risk per trade position sizing","informational","middle","guide",1),
("How XeanVI Uses Playbook Rules to Support Trading Discipline","xeanvi playbook rules discipline","informational","bottom","product education",2),
]

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    with app.app_context():
        if BlogPublishingPlan.query.count() and not args.force:
            print('BlogPublishingPlan rows exist; skipping. Use --force to add starters.')
            raise SystemExit(0)
        existing = {norm(p.title) for p in BlogPublishingPlan.query.all()}
        base = next_monday(date.today())
        created = 0
        for i, (title, kw, intent, funnel, ctype, prio) in enumerate(TOPICS):
            if norm(title) in existing:
                continue
            db.session.add(BlogPublishingPlan(title=title, target_keyword=kw, search_intent=intent, funnel_stage=funnel, content_type=ctype, priority=prio, status='queued', planned_publish_date=base + timedelta(days=7*i), notes='Educational angle only. Mention risk management and paper trading where relevant. Add internal links to playbook, onboarding, and blog resources.'))
            created += 1
        db.session.commit()
        print(f'Created {created} starter blog rhythm topics.')
