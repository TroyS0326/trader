# SEO Blog Publishing Rhythm

## Recommended cadence
- 1 post per week minimum.
- 2 posts per week once workflow is stable.
- Refresh older posts monthly.

## Weekly workflow
- Monday: pick topic and target keyword.
- Tuesday: draft content.
- Wednesday: add internal links and run compliance/risk review.
- Thursday: SEO polish, meta title/description, canonical URL, image alt text.
- Friday: publish or schedule.

## Monthly workflow
- Audit rankings and traffic using external analytics tools.
- Refresh older posts for accuracy and relevance.
- Add internal links from older posts to newer posts.

## Content safety rules
- No financial advice.
- No profit promises.
- No guaranteed claims.
- Mention risk management and paper trading where relevant.

## Suggested topic clusters
- Trading Playbook
- Paper Trading
- Risk Management
- Broker API Integration
- ORB/VWAP Education
- Trading Automation Discipline
- Bracket Orders
- XeanVI Product Education

## Admin rhythm workflow
Use `/admin/blog-rhythm` (admin only, direct URL) to:
1. Maintain a topic calendar.
2. Add and track planned topics.
3. Create drafts without auto-publishing.
4. Review quality and SEO readiness before publishing.

Checklist items:
- Draft created
- SEO score checked
- Risk claims checked
- Internal links added
- Meta title/description completed
- Canonical URL present
- Featured image and alt text added (if available)
- Admin reviewed

## Seed starter topics
Run:

```bash
python scripts/seed_blog_rhythm.py
```

Use `--force` to allow adding missing starter topics even when rows already exist.

Reminder: Admin must review every AI draft before publishing.
