# YT Publishing Dream Team

Standalone web app: paste one YouTube URL, get a complete Publishing Artifact
(title, description, tags, hashtags, pinned comment, thumbnail prompts,
chapters, EN captions + 25-language localization) with per-field copy buttons.

- Free tier: Google Sign-In, 2 artifacts/day, 10/month.
- Stack: FastAPI - Railway - Neon Postgres (pooled, statement_cache_size=0; rate limiting + cache in Postgres, no Redis).
- Branches: dev (work) / main (prod, ff-merge only).
- Domain: dreamteam.commentclient.com

Run locally:
    pip install -r requirements.txt
    uvicorn app.main:app --reload
