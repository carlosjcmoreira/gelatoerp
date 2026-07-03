---
name: Deployment target decisions
description: Tracks decisions about autoscale vs VM deployment target and the cold-start tradeoff, so the topic isn't re-litigated from scratch each session.
---

## Cold start / "servidor a acordar" tradeoff

The app was published on `autoscale`, which hibernates after inactivity and shows a "O servidor está a acordar" loading message on the first request after sleep (see `flask_app/templates/base.html` loading-overlay logic).

- 2026-05-20: user proposed switching to `vm` (always-on) to eliminate cold start, but then cancelled that change (chose to keep `autoscale`, accepting the tradeoff for its lower/variable cost).
- 2026-07-03: user revisited the topic and asked again about eliminating cold start; confirmed switching to `vm` this time, accepting the fixed monthly cost in exchange for no cold start.

**Why this matters:** this decision has flip-flopped once already. Don't assume the current deployment target is intentional/final — check `getDeploymentInfo()` for the live target and check this file for the latest reasoning before proposing changes again.

**How to apply:** when the user asks about slow first-load, "servidor a acordar", or deployment cost/performance tradeoffs, reference this history instead of re-deriving the tradeoff from scratch, and update this note if the decision changes again.
