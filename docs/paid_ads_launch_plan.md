# Paid Ads Launch Plan (14 Days)

## Positioning Decision
Do **not** run follower campaigns first. Prioritize qualified traffic that can convert to signup and checkout.

## 14-Day Campaign Structure
- Google Search Campaign A (high intent exact/phrase)
- Google Search Campaign B (competitor/category discovery; optional)
- Meta Campaign A (retargeting site visitors and engaged users)
- Meta Campaign B (creative testing optimized for lead/conversion, not followers)

## Budget Split
- 60-70% Google Search high-intent
- 20-30% Meta retargeting/conversion
- 10% testing and creative experiments

## Objectives
- Google: optimize for conversions once tracking is verified; only brief Max Clicks ramp with daily search term review.
- Meta: leads/sales/conversions once signal is sufficient; follower objective is not core strategy.

## Safe Ad Copy Guardrails
- No guaranteed returns.
- No "AI finds winning stocks."
- No "beat Wall Street."
- No profit screenshots.
- No unrealistic lifestyle claims.

## Suggested Google Keywords
- "trading automation software"
- "rule based trading software"
- "paper trading automation"
- "automated trading risk controls"
- "trading bot paper testing"

## Negative Keywords
free, guaranteed, get rich, passive income, no risk, signals, pump, crypto millionaire, casino, binary options, forex signals, CFD

## Meta Creative Concepts
- Paper mode first
- Rules before routing
- Stop overriding your playbook
- Broker permissions stay under your control

## Compliance Reminders
- Use financial products/services special category where required.
- Avoid investment advice language.
- Keep fees, address, and risk disclosures visible on landing pages.
- Avoid targeting minors.

## Measurement Plan
- Primary KPI: checkout started and paid subscription
- Secondary KPI: signup completion
- Reject KPI: followers, raw traffic, cheap clicks

## Kill Rules
- Pause ad sets with spend >2x target CPA and no signup/checkout.
- Pause creatives with high CTR but no signup.
- Review and add search-term negatives daily in week 1.

## Safe Ad Copy Examples

### Google Search Headlines
- Rule-Based Trading Software
- Test in Paper Mode First
- User-Defined Trading Rules
- Broker Permissions Stay With You
- Risk Controls Before Live Use
- Trading Workflow Visibility

### Google Search Descriptions
- Build your own trading rules, test in paper mode, and monitor each step before live routing.
- XeanVI is software for rule-based workflow automation with broker-permission controls and audit visibility.
- Start with paper testing, configure risk settings, and use supported broker-connected tools when ready.

### Meta Primary Text
- XeanVI helps you structure your process with user-defined rules, paper mode testing, and risk controls before live use.
- Keep broker permissions in your control while using software to run a rules-based workflow you can monitor.
- Built for traders who want documented process, audit visibility, and paper-first validation.

### Meta Headline
- Paper-First Trading Workflow Software

### Meta Description
- Rule-based software with paper mode, risk controls, and broker-permission safeguards.

## Conversion Tracking Readiness

### What is tracked now
- **Paid landing CTA to signup intent:** `/lp/rule-based-trading-automation` links carry UTM parameters, but CTA clicks are **not** counted as completed signup conversions.
- **Completed signup (not click or form submit):** successful account creation redirects planned buyers to `/pricing?plan=...&signup_success=1`, and one-time client-side guards fire:
  - Meta Pixel `CompleteRegistration`
  - Google Ads conversion key `signup`
- **Signup form submit is not completed signup:** form submission alone does not fire completed-signup conversion events; conversion fires only after successful account creation via `signup_success=1`.
- **Checkout started:** monthly and annual pricing checkout forms fire:
  - Meta Pixel `InitiateCheckout`
  - Google Ads conversion key `checkout` with value/currency metadata
  - Existing callback + hard-timeout fallback so conversion callbacks do not permanently block Stripe redirect.
- **Paid subscription truth source:** server-side Stripe events continue recording `checkout.completed` / `invoice.paid` via existing `UserEvent` tracking.

### What is not tracked as a browser conversion yet
- A browser-side Google Ads/Meta **purchase** event is **not** currently emitted from a dedicated post-payment page because checkout finalization currently returns users to setup flow without a dedicated conversion-render page. This avoids misleading purchase conversion firing.
- Do not claim purchase browser conversions are live until a deterministic browser success destination is implemented and verified.

### Must-verify before ad spend
- In **Google Ads**:
  - Confirm `signup` and `checkout` conversion actions are receiving events and mapped to the correct labels.
  - Confirm enhanced conversion diagnostics (if enabled) are healthy.
- In **Meta Events Manager**:
  - Confirm `CompleteRegistration` and `InitiateCheckout` are received on intended steps.
  - Validate event dedup/quality and no unusual repeated refresh firing.
- Run real QA flows from ad-style URLs with UTMs through signup and checkout start.

### Optimization guidance
- Start optimization on **completed signup** (or **checkout started** if signup volume is noisy).
- Google Ads optimization should target completed signup or checkout-start, **not** signup button clicks.
- Move primary optimization to **paid subscription/purchase** only when reliable conversion volume exists and truthful purchase measurement is verified.
- Do **not** optimize on raw clicks, traffic, or follower counts.

