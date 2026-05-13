# XeanVI Viral Prompt System

## Purpose

This document converts the Viral character charter into reusable prompt templates for blog images, social media images, short-form video concepts, captions, alt text, image captions, and metadata support.

Use this system whenever creating XeanVI content that includes Viral. The goal is visual consistency, brand trust, and compliance-safe educational positioning.

---

## Required source document

This prompt system depends on:

```txt
docs/brand/viral-character-charter.md
```

Do not use these prompts in a way that conflicts with the character charter.

---

## Core character lock

Use this character lock in every Viral image or video prompt.

```txt
Viral is the official AI-generated XeanVI brand character: a semi-realistic futuristic female AI fintech educator. She appears serious, direct, educational, and approachable. She is professional, calm, credible, and risk-aware. She is not sexualized, not cartoonish, not a stock picker, not a profit-promising influencer, and not based on any real person.
```

---

## Core visual lock

Use this visual lock in every Viral image prompt.

```txt
Dark premium fintech/SaaS aesthetic, clean trading operations environment, dark navy and charcoal palette, subtle cyan and teal highlights, realistic cinematic lighting, muted dashboard interfaces, rule-based automation visuals, risk-control panels, paper trading or broker workflow context when relevant, center-safe composition, no embedded text, no logos, no watermarks.
```

---

## Core negative prompt

Use this negative prompt unless a specific tool does not support negative prompts.

```txt
sexualized, seductive pose, revealing outfit, lingerie, glamour model, luxury car, mansion, money pile, cash, fake account balance, profit guarantee, stock pick, buy signal, sell signal, price target, casino, gambling, cartoon, anime, mascot, childish, exaggerated expression, chaotic chart, messy dashboard, blurry, low resolution, text, words, logo, watermark, real celebrity likeness, real person likeness
```

---

## Compliance line bank

Use one of these lines in captions or accompanying copy when appropriate.

```txt
AI-generated brand character. Not financial advice. Trading involves risk.
```

```txt
XeanVI supports rule-based trading workflows. It does not guarantee profits or remove market risk.
```

```txt
Educational content only. No stock picks, trade recommendations, or guaranteed outcomes.
```

```txt
Automation can help enforce predefined rules. It cannot make trading risk disappear.
```

---

## Blog image prompt template

Use this for blog hero images or supporting blog graphics.

```txt
Create a 1200x675 px PNG image, 16:9, sRGB, under 5MB. Viral is the official AI-generated XeanVI brand character: a semi-realistic futuristic female AI fintech educator. She appears serious, direct, educational, and approachable. She is shown in a clean dark fintech trading operations environment related to [BLOG TOPIC]. Use subtle dashboard interfaces, rule-based automation visuals, risk-control panels, and muted candlestick charts in the background. Dark navy and charcoal palette with cyan and teal highlights. Professional futuristic blazer or clean techwear. Realistic cinematic lighting. Premium SaaS visual style. Center-safe composition. No embedded text, no logos, no watermarks, no profit claims, no stock recommendations, no luxury lifestyle imagery, no seductive pose, no real-person likeness.
```

### Blog image variables

Replace `[BLOG TOPIC]` with one of:

- trading playbook discipline
- rule-based trading automation
- trading bot versus automation platform
- paper trading validation
- broker API connection workflow
- risk management before execution
- bracket order planning
- emotional trading control
- automated scanning and validation
- execution discipline for retail traders

---

## Social image prompt template

Use this for X/Twitter, Facebook, LinkedIn, or Instagram feed images.

```txt
Create a professional social media image featuring Viral, the official AI-generated XeanVI brand character. Semi-realistic futuristic female AI fintech educator, serious, direct, educational, and approachable. She is positioned in a clean trading automation command-center environment with subtle risk controls, playbook rule visuals, paper trading indicators, and broker connection UI elements in the background. Dark navy and charcoal fintech palette with cyan and teal highlights. Premium SaaS aesthetic, realistic lighting, center-safe framing, no embedded text, no logos, no profit claims, no stock picks, no seductive pose, no luxury lifestyle imagery, no real-person likeness.
```

### Recommended sizes

Use the correct size for the platform:

| Platform | Recommended size | Format |
| --- | --- | --- |
| Blog hero | 1200x675 | PNG or JPG |
| X/Twitter feed | 1600x900 | PNG or JPG |
| Facebook feed | 1200x630 | PNG or JPG |
| LinkedIn feed | 1200x627 | PNG or JPG |
| Instagram square | 1080x1080 | PNG or JPG |
| Instagram/Reels cover | 1080x1920 | PNG or JPG |
| YouTube Shorts/TikTok/Reels | 1080x1920 | MP4 for video, PNG for cover |

---

## Short-form video prompt template

Use this for external AI video tools.

```txt
Create a short vertical 1080x1920 fintech educational video featuring Viral, the official AI-generated XeanVI brand character. Viral is a semi-realistic futuristic female AI fintech educator. She appears serious, direct, educational, and approachable. She stands in a dark premium trading operations workspace with subtle holographic-style interfaces showing rule-based automation, risk controls, paper trading validation, and broker workflow concepts. Cinematic lighting, dark navy and charcoal palette with cyan and teal highlights. Calm professional delivery, no hype, no stock recommendations, no profit claims, no luxury lifestyle imagery, no seductive pose, no real-person likeness. Social-media-ready composition.
```

### Video script structure

Every Viral video script should follow this order:

1. Hook
2. Problem
3. Principle
4. XeanVI workflow connection
5. Risk-aware close

### Example 20-second script

```txt
Hook: Most traders do not need more random signals.
Problem: They need a process that stops changing when pressure hits.
Principle: A playbook defines the rules before the trade exists.
XeanVI connection: XeanVI is built around that workflow: define the rule, test it in paper mode, validate the setup, then automate execution support.
Close: Automation does not remove risk. It helps enforce the rules you already approved.
```

---

## Caption template

Use this for Viral social captions.

```txt
[DIRECT HOOK]

[TRADER PROBLEM]

[DISCIPLINED PRINCIPLE]

[XEANVI WORKFLOW CONNECTION]

[COMPLIANCE LINE]
```

### Example caption

```txt
Most traders do not need more random signals.

They need a process that does not change when pressure hits.

A trading playbook defines the rule before the trade exists. That rule should control entry logic, risk, exits, and whether the setup is even valid.

XeanVI is built for that workflow: define the playbook, test in paper mode, validate the setup, and automate execution support around approved rules.

AI-generated brand character. Not financial advice. Trading involves risk.
```

---

## Blog metadata helper template

Use this when creating Viral-supported blog assets.

```txt
Target keyword: [TARGET KEYWORD]
Blog topic: [BLOG TOPIC]
Viral image filename: [seo-keyword-viral-xeanvi.png]
ALT text: Semi-realistic AI-generated XeanVI character Viral explaining [BLOG TOPIC] in a dark fintech trading automation workspace.
Image caption: Viral explains how [BLOG TOPIC] connects to disciplined, rule-based trading automation inside XeanVI.
Meta description: Learn how [BLOG TOPIC] supports disciplined trading workflows, risk controls, paper testing, and rule-based automation with XeanVI.
Excerpt: A practical look at [BLOG TOPIC] and why structured rules, risk controls, and automation matter for retail traders using XeanVI.
Meta title: [TARGET KEYWORD] | XeanVI
```

---

## Filename rules

Use lowercase, hyphen-separated, SEO-readable filenames.

### Good filenames

```txt
trading-playbook-discipline-viral-xeanvi.png
rule-based-trading-automation-viral-xeanvi.png
trading-bot-vs-automation-platform-viral-xeanvi.png
paper-trading-validation-viral-xeanvi.png
broker-api-workflow-viral-xeanvi.png
risk-management-before-execution-viral-xeanvi.png
```

### Bad filenames

```txt
image1.png
viralhotgirl.png
ai-influencer-final-final.png
trading-profit-guarantee.png
stock-picks-ai.png
```

---

## Prompt variants by content pillar

### 1. Trading playbook discipline

```txt
Create a 1200x675 px PNG image, 16:9, sRGB, under 5MB. Viral, the official AI-generated XeanVI brand character, stands beside a clean abstract trading playbook interface showing rule blocks, entry criteria, risk settings, and exit planning without readable text. Semi-realistic futuristic female AI fintech educator, serious and direct, approachable and educational. Dark premium fintech workspace, navy and charcoal palette, cyan and teal highlights, realistic cinematic lighting, no embedded text, no logos, no profit claims, no stock picks, no seductive pose, no real-person likeness.
```

### 2. Risk management before execution

```txt
Create a 1200x675 px PNG image, 16:9, sRGB, under 5MB. Viral, the official AI-generated XeanVI brand character, reviews a futuristic risk-control dashboard with position sizing, stop-loss planning, bracket order structure, and portfolio heat concepts represented visually without readable text. Semi-realistic futuristic female AI fintech educator, serious and calm, professional fintech style. Dark navy and charcoal trading operations room, cyan and teal highlights, realistic lighting, no embedded text, no logos, no profit claims, no stock picks, no gambling imagery, no seductive pose, no real-person likeness.
```

### 3. Paper trading validation

```txt
Create a 1200x675 px PNG image, 16:9, sRGB, under 5MB. Viral, the official AI-generated XeanVI brand character, is shown in a paper trading simulation environment with clean validation panels, simulated order flow, rule check indicators, and muted chart visuals without readable text. Semi-realistic futuristic female AI fintech educator, direct and educational, approachable but serious. Dark premium SaaS/trading aesthetic, navy charcoal cyan teal palette, realistic cinematic lighting, no embedded text, no logos, no profit claims, no stock picks, no seductive pose, no real-person likeness.
```

### 4. Broker API workflow

```txt
Create a 1200x675 px PNG image, 16:9, sRGB, under 5MB. Viral, the official AI-generated XeanVI brand character, explains a secure broker API connection workflow represented by abstract encrypted connection lines between a trading dashboard and broker interface, with no readable text. Semi-realistic futuristic female AI fintech educator, serious, professional, approachable. Clean dark fintech interface, navy and charcoal palette with cyan and teal highlights, realistic lighting, no embedded text, no logos, no real broker branding, no profit claims, no stock picks, no seductive pose, no real-person likeness.
```

### 5. Trading bot versus automation platform

```txt
Create a 1200x675 px PNG image, 16:9, sRGB, under 5MB. Viral, the official AI-generated XeanVI brand character, stands between two abstract automation concepts: one simple single-signal bot icon on one side and a larger connected execution platform workflow on the other side, including playbook rules, risk controls, paper testing, scanner validation, and broker connection visuals without readable text. Semi-realistic futuristic female AI fintech educator, serious and direct. Premium dark fintech SaaS style, navy charcoal cyan teal palette, realistic lighting, no embedded text, no logos, no profit claims, no stock picks, no seductive pose, no real-person likeness.
```

### 6. Automated scanning and validation

```txt
Create a 1200x675 px PNG image, 16:9, sRGB, under 5MB. Viral, the official AI-generated XeanVI brand character, monitors an automated market scanning and validation command center with abstract watchlist cards, rule checks, risk filters, and muted chart panels without readable text. Semi-realistic futuristic female AI fintech educator, calm, serious, direct, educational. Clean dark trading operations environment, navy charcoal cyan teal palette, realistic cinematic lighting, no embedded text, no logos, no profit claims, no stock picks, no seductive pose, no real-person likeness.
```

---

## Asset approval checklist

Approve a Viral asset only if all answers are yes:

- Does Viral look professional enough for a trading automation platform?
- Does the image avoid sexualized influencer styling?
- Does the image avoid fake wealth signals?
- Does the image avoid profit promises or trade recommendations?
- Does the image fit XeanVI's dark fintech/SaaS visual language?
- Does the content reinforce discipline, rules, risk, paper testing, broker workflow, or automation?
- Is the composition usable for blog/social cropping?
- Is there no embedded text unless intentionally added later by the design system?
- Would this make XeanVI look more trustworthy?

Reject the asset if any answer is no.

---

## First 10 asset concepts

Use these as the first batch for testing Viral consistency.

1. Viral explaining a trading playbook rule stack.
2. Viral reviewing a risk-control dashboard.
3. Viral beside a paper trading simulation workflow.
4. Viral showing a secure broker API connection concept.
5. Viral comparing a simple bot to a full automation platform.
6. Viral monitoring automated scanning and validation panels.
7. Viral in a clean trading operations command center.
8. Viral explaining bracket order planning visually.
9. Viral showing emotional trading control through rule gates.
10. Viral presenting a beginner-friendly automation workflow.

---

## Hard rule

Do not generate Viral assets that make XeanVI look like a hype trading product. Viral must make XeanVI look more disciplined, more trustworthy, and more operationally serious.
