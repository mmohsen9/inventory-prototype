# Production Deployment, Security, and Continuous Evolution Plan

See full Arabic specification in [خطة-النشر-والتأمين-والتطوير-المستمر.md](file:///d:/%D9%85%D8%AC%D9%84%D8%AF%20%D8%AC%D8%AF%D9%8A%D8%AF/OneDrive/scratch/inventory-prototype/%D8%AE%D8%B7%D8%A9-%D8%A7%D9%84%D9%86%D8%B4%D8%B1-%D9%88%D8%A7%D9%84%D8%AA%D8%A3%D9%85%D9%8A%D9%86-%D9%88%D8%A7%D9%84%D8%AA%D8%B7%D9%88%D9%8A%D8%B1-%D8%A7%D9%84%D9%85%D8%B3%D8%AA%D9%85%D8%B1.md).

## Status: ON HOLD
Awaiting the user's provision of the real Foodics Production API credentials.

## Architectural Key Decisions (Approved):
1. **Target Platform:** Render Web Service (Starter Plan, ~$7/mo) + Persistent Disk (10 GB @ $0.25/GB = $2.50/mo). Fixed predictable cost ~$9.50/mo.
2. **Forbidden:** Render Free Tier (ephemeral disk loses SQLite database and uploads on sleep/redeploy).
3. **Region:** Frankfurt, Germany (`frankfurt`) - lowest latency to Riyadh, Saudi Arabia (~60-80ms RTT).
4. **Persistent Disk Trade-off:** Disables zero-downtime deploys and limits service to 1 instance. Ideal for SQLite concurrency safety.
5. **Execution Order:** Phased execution with acceptance gates. Do not proceed to the next phase without explicit user approval.
6. **Continuous Evolution (Phase 6):** Safe migrations with Flask-Migrate (Alembic batch mode for SQLite), isolated local dev environment, feature branches, and zero-data-loss upgrades.
