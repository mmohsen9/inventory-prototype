# Production Deployment and Continuous Evolution Rules

## Status: WAITING FOR PRODUCTION FOODICS API
Do NOT begin deployment execution until the user explicitly provides the Foodics Production API Token and instructs to start.

## Deployment Architecture & Rules:
1. **Target:** Render Web Service (Starter Plan) + Persistent Disk (mounted to `/var/data`).
2. **Region:** Frankfurt, Germany (`frankfurt`).
3. **Never use Free Tier:** Ephemeral storage on Free tier causes total loss of SQLite database and uploads.
4. **Single Instance Only:** Persistent disk enforces 1 instance, which is required for SQLite concurrency safety.
5. **No Scope Creep:** No managed Postgres, no Redis, no background worker services without explicit written user approval.
6. **Execution Protocol:** Work phase-by-phase. Stop at each acceptance gate and wait for user approval.
7. **Future Development Protocol:** All future schema changes must use Flask-Migrate with `render_as_batch=True` to safely migrate SQLite without losing production data.

Full specification document: [خطة-النشر-والتأمين-والتطوير-المستمر.md](file:///d:/%D9%85%D8%AC%D9%84%D8%AF%20%D8%AC%D8%AF%D9%8A%D8%AF/OneDrive/scratch/inventory-prototype/%D8%AE%D8%B7%D8%A9-%D8%A7%D9%84%D9%86%D8%B4%D8%B1-%D9%88%D8%A7%D9%84%D8%AA%D8%A3%D9%85%D9%8A%D9%86-%D9%88%D8%A7%D9%84%D8%AA%D8%B7%D9%88%D9%8A%D8%B1-%D8%A7%D9%84%D9%85%D8%B3%D8%AA%D9%85%D8%B1.md).
