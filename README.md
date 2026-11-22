# Aiprojects4U content assistant

Lets Claude add and edit courses, internships, projects, blog articles and free course lessons on aiprojects4u.com, check SEO, and publish, from your phone. It writes the same files as the `/admin` dashboard, so both stay in sync. `CLAUDE.md` tells Claude how to work here.

## Phone setup (Claude Code on the web)
Claude runs in the cloud on this repo, so your computer can be off.

1. **Put this folder on GitHub as a private repo** (e.g. `aip-content-manager`).
2. **Create an R2 token.** Go to Cloudflare → R2 → Manage API tokens → Create. Choose **Object Read & Write**, limited to the buckets `aip-public` and `aip-content`.
3. **Open claude.ai/code in a browser** (once, it's easier than the phone). Connect GitHub and give it access to this repo (and `website` if you want code/SEO template fixes too).
4. **Create a cloud environment** (environment settings when starting a session), e.g. named `aiprojects4u`:
   - **Network access:** Custom. Allow `*.r2.cloudflarestorage.com` and `aiprojects4u.com` (plus `www.aiprojects4u.com` if you use it).
   - **Environment variables:**
     ```
     R2_ACCOUNT_ID=...
     R2_ACCESS_KEY_ID=...
     R2_SECRET_ACCESS_KEY=...
     ```
5. **On your phone:** Claude app → **Code** → pick this repo and the `aiprojects4u` environment → type what you want.

Examples:
- "Add a new internship: Data Engineering, 8 weeks, ₹9,999, offer ₹5,999, starts 15 Jan 2027"
- "Write a draft article about how to choose a final-year AI project, link to our internships"
- "Publish the RAG article"
- "Run an SEO check of the live site and fix the top 5 content issues"
- "Lower all project offer prices by ₹1,000"

Claude pulls the latest content, edits it, validates it, shows you the exact changes, and publishes only after you say yes.

## Local use (optional)
`cp .env.example .env`, fill it in, then `python3 aip.py pull`. Run `python3 aip.py` to see all commands. This also works from the Claude desktop app (Cowork) with this folder open.

## Safety
- Every push refuses if someone edited the same file in the admin since the pull.
- Every push first saves the current live version to R2 (`aip-content/backups/`): `python3 aip.py backups` and `restore`.
- Article HTML is sanitized with the same rules as the admin (no scripts or event handlers).
- Keys live only in the environment / `.env`, never in the repo.
