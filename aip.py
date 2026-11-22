#!/usr/bin/env python3
"""Manage aiprojects4u.com content: courses, internships, projects and blog articles.

    python3 aip.py pull                  download live content into content/
    python3 aip.py list [courses|internships|projects|articles]
    python3 aip.py validate              check content/ for mistakes
    python3 aip.py diff                  show what push would change
    python3 aip.py push                  dry run: validate + show the changes
    python3 aip.py push --yes            publish (live in about a minute)
    python3 aip.py seo                   SEO audit of the pulled content
    python3 aip.py seo --live            SEO crawl of the live site (via its sitemap)
    python3 aip.py upload-image FILE     upload an image, print its URL
    python3 aip.py backups               list backups taken before each push
    python3 aip.py restore NAME          put a backup into content/ (then push to publish it)

Writes exactly what the /admin dashboard writes. Python 3.8+, standard library only.
Settings come from environment variables or .env (see .env.example).
"""
import json
import mimetypes
import os
import sys
import time
from pathlib import Path

from aiptool import articles as art
from aiptool import catalog as cat
from aiptool import seo
from aiptool.r2 import R2, R2Error

HERE = Path(__file__).resolve().parent
CONTENT = HERE / "content"
ARTICLES = CONTENT / "articles"
STATE = CONTENT / ".pulled.json"   # what was pulled (versions + ETags), for diff and safe writes
LOCAL_BACKUPS = HERE / "backups"

KEYS = {  # same object keys the site and admin use
    "courses": "content/courses.json",
    "site": "content/site.json",
    "redirects": "content/redirects.json",
    "public_article_index": "content/articles/index.json",
    "public_article": "content/articles/{slug}.json",
    "article_index": "articles/index.json",          # private bucket (drafts included)
    "article": "articles/{id}.json",                 # private bucket
}
PUBLIC_CACHE = "public, max-age=60"
JSON_TYPE = "application/json; charset=utf-8"


# ------------------------------------------------------------------ config

def config():
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    env = os.environ
    return {
        "account": env.get("R2_ACCOUNT_ID", ""),
        "key_id": env.get("R2_ACCESS_KEY_ID", ""),
        "secret": env.get("R2_SECRET_ACCESS_KEY", ""),
        "endpoint": env.get("R2_ENDPOINT") or f"https://{env.get('R2_ACCOUNT_ID', '')}.r2.cloudflarestorage.com",
        "public": env.get("R2_PUBLIC_BUCKET", "aip-public"),
        "private": env.get("R2_PRIVATE_BUCKET", "aip-content"),
        "public_url": env.get("PUBLIC_URL", "https://media.aiprojects4u.com").rstrip("/"),
        "site_url": env.get("SITE_URL", "https://aiprojects4u.com").rstrip("/"),
    }


def client(cfg):
    missing = [n for n, k in (("R2_ACCOUNT_ID", "account"), ("R2_ACCESS_KEY_ID", "key_id"), ("R2_SECRET_ACCESS_KEY", "secret")) if not cfg[k]]
    if missing:
        sys.exit(f"Missing {', '.join(missing)}. Set them as environment variables or in .env (see .env.example).")
    return R2(cfg["endpoint"], cfg["key_id"], cfg["secret"])


# ------------------------------------------------------------------ local files

def dump(data, pretty=True):
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n" if pretty
            else json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def read_json(name):
    path = CONTENT / name
    if not path.exists():
        sys.exit(f"content/{name} not found. Run `python3 aip.py pull` first.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        sys.exit(f"content/{name} is not valid JSON: line {err.lineno}, column {err.colno}: {err.msg}")


def write_article_file(article):
    for old in ARTICLES.glob(f"{article['id']}-*.html"):
        old.unlink()
    (ARTICLES / art.file_name(article)).write_text(art.render_file(article), encoding="utf-8")


def load_state():
    if not STATE.exists():
        sys.exit("Nothing pulled yet. Run `python3 aip.py pull` first.")
    state = json.loads(STATE.read_text())
    state["articles"] = {int(k): v for k, v in state["articles"].items()}
    return state


def save_state(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False))


def load_local():
    programs, site = read_json("courses.json"), read_json("site.json")
    if not isinstance(programs, list):
        sys.exit("content/courses.json must be a JSON list.")
    if not isinstance(site, dict):
        sys.exit("content/site.json must be a JSON object.")
    site.setdefault("projects", [])
    local_articles, errors = [], []
    for path in sorted(ARTICLES.glob("*.html")):
        try:
            local_articles.append((path.name, art.parse_file(path)))
        except ValueError as err:
            errors.append(str(err))
    return programs, site, local_articles, errors


# ------------------------------------------------------------------ change detection

def article_changes(state, local_articles):
    """[(kind, name, local, existing)] with kind new | changed | deleted."""
    existing = state["articles"]
    changes, seen = [], set()
    for name, a in local_articles:
        if a.get("id") is None:
            changes.append(("new", name, a, None))
            continue
        seen.add(a["id"])
        before = existing.get(a["id"])
        if before is None:
            changes.append(("unknown", name, a, None))
        elif art.comparable(before) != art.comparable(a):
            changes.append(("changed", name, a, before))
    for aid, before in existing.items():
        if aid not in seen:
            changes.append(("deleted", f"{aid}-{before.get('slug')}", None, before))
    return changes


def describe_articles(changes):
    lines = []
    for kind, name, a, before in changes:
        if kind == "new":
            lines.append(f"  + new article: {a.get('title')}  ({'will be PUBLISHED' if a.get('published') else 'draft'})")
        elif kind == "changed":
            fields = sorted(k for k, v in art.comparable(a).items() if art.comparable(before).get(k) != v)
            lines.append(f"  ~ article {before['id']} {a.get('title')}: {', '.join(fields)}")
            if a.get("published") and not before.get("published"):
                lines.append("      PUBLISH: goes live")
            elif before.get("published") and not a.get("published"):
                lines.append(f"      UNPUBLISH: /blog/{before['slug']} will return 404")
            elif before.get("published") and "slug" in fields and a.get("slug"):
                lines.append(f"      URL /blog/{before['slug']} -> /blog/{art.normalize_slug(a['slug'])} (301 redirect added)")
        elif kind == "deleted":
            lines.append(f"  - DELETE article {before['id']} {before.get('title')}"
                         + (f"  (/blog/{before['slug']} will return 404)" if before.get("published") else " (draft)"))
    return lines


def changes_summary(state, programs, site, local_articles):
    old_site = state["data"]["site"] or {}
    lines = cat.describe_changes("program", state["data"]["courses"] or [], programs, cat.program_path)
    lines += cat.describe_changes("project", old_site.get("projects") or [], site["projects"], cat.project_path)
    other = sorted(k for k in set(old_site) | set(site) if k != "projects" and old_site.get(k) != site.get(k))
    if other:
        lines.append(f"  ~ site.json sections changed: {', '.join(other)}")
    lines += describe_articles(article_changes(state, local_articles))
    return lines


# ------------------------------------------------------------------ commands

def cmd_pull(args):
    cfg = config()
    r2 = client(cfg)
    if STATE.exists() and "--force" not in args:
        programs, site, local_articles, _ = load_local()
        if changes_summary(load_state(), programs, site, local_articles):
            sys.exit("content/ has edits that were not pushed. Push them, or run `pull --force` to throw them away.")
    data, etags = {}, {}
    for name in ("courses", "site", "redirects"):
        data[name], etags[name] = r2.get_json(cfg["public"], KEYS[name])
    data["article_index"], etags["article_index"] = r2.get_json(cfg["private"], KEYS["article_index"])
    full = {}
    for summary in data["article_index"] or []:
        article, _ = r2.get_json(cfg["private"], KEYS["article"].format(id=summary["id"]))
        if article:
            full[int(article["id"])] = article

    CONTENT.mkdir(exist_ok=True)
    ARTICLES.mkdir(exist_ok=True)
    for old in ARTICLES.glob("*.html"):
        old.unlink()
    (CONTENT / "courses.json").write_text(dump(data["courses"] or []), encoding="utf-8")
    (CONTENT / "site.json").write_text(dump(data["site"] or {}), encoding="utf-8")
    for article in full.values():
        write_article_file(article)
    save_state({"pulledAt": time.strftime("%Y-%m-%d %H:%M:%S"), "etags": etags, "data": data, "articles": full})

    programs = data["courses"] or []
    print(f"Pulled: {sum(p.get('type') == 'internship' for p in programs)} internships, "
          f"{sum(p.get('type') != 'internship' for p in programs)} courses, "
          f"{len((data['site'] or {}).get('projects') or [])} projects, {len(full)} articles "
          f"({sum(bool(a.get('published')) for a in full.values())} published).")
    if not data["site"]:
        print("Note: site.json doesn't exist in R2 yet; the site shows built-in sample content until the first save.")


def cmd_list(args):
    which = args[0] if args else "all"
    programs, site, local_articles, _ = load_local()
    rows = []
    if which in ("all", "courses", "internships"):
        rows += [(p.get("type", "?"), p, cat.program_path(p)) for p in programs
                 if which == "all" or p.get("type") == which.rstrip("s")]
    if which in ("all", "projects"):
        rows += [("project", p, cat.project_path(p)) for p in site["projects"]]
    for kind, p, path in rows:
        price = p.get("discountPrice") or p.get("originalPrice") or 0
        print(f"{kind:10} id={str(p.get('id')):4} ₹{price:<7} {p.get('title')}  [{path}]{'  ★' if p.get('featured') else ''}")
    if which in ("all", "articles"):
        for name, a in local_articles:
            status = "live " if a.get("published") else "draft"
            print(f"article    {status}    {a.get('title')}  [content/articles/{name}]")
        rows += local_articles
    print(f"{len(rows)} item(s)")


def run_validation(programs, site, local_articles, parse_errors):
    errors, warnings = cat.validate(programs, site["projects"])
    a_errors, a_warnings = art.validate(local_articles)
    state = load_state()
    errors += parse_errors + a_errors
    errors += [f"article {name}: id {a['id']} doesn't exist in R2 (maybe deleted in the admin). Remove the id line to create it as a new article."
               for kind, name, a, _ in article_changes(state, local_articles) if kind == "unknown"]
    return errors, warnings + a_warnings


def cmd_validate(_args):
    errors, warnings = run_validation(*load_local())
    for w in warnings:
        print(f"  warning: {w}")
    for e in errors:
        print(f"  ERROR: {e}")
    print("OK, no errors." if not errors else f"{len(errors)} error(s). Fix them before pushing.")
    return not errors


def cmd_diff(_args):
    programs, site, local_articles, _ = load_local()
    lines = changes_summary(load_state(), programs, site, local_articles)
    print("\n".join(lines) if lines else "No changes.")


def cmd_push(args):
    if not cmd_validate(args):
        sys.exit(1)
    cfg = config()
    state = load_state()
    programs, site, local_articles, _ = load_local()
    old_programs = state["data"]["courses"] or []
    old_site = state["data"]["site"] or {}
    old_projects = old_site.get("projects") or []
    cat.finalize(programs, site["projects"], old_programs, old_projects)

    lines = changes_summary(state, programs, site, local_articles)
    print("Changes:")
    if not lines:
        print("  none. Nothing to push.")
        return
    print("\n".join(lines))
    if "--yes" not in args:
        print("\nDry run. Nothing was written. Run `python3 aip.py push --yes` to publish.")
        return
    r2 = client(cfg)

    # ---- work out every write first
    redirects = list(state["data"]["redirects"] or [])
    redirects = cat.redirects_for(old_programs, programs, cat.program_path, redirects)
    redirects = cat.redirects_for(old_projects, site["projects"], cat.project_path, redirects)

    changes = article_changes(state, local_articles)
    index = list(state["data"]["article_index"] or [])
    saved, removed_public, removed_private = [], [], []
    for kind, _, a, before in changes:
        if kind == "deleted":
            index = [s for s in index if s.get("id") != before["id"]]
            removed_private.append(before["id"])
            if before.get("published"):
                removed_public.append(before["slug"])
            continue
        article = art.apply(index, before, a)
        pos = next((i for i, s in enumerate(index) if s.get("id") == article["id"]), None)
        if pos is None:
            index.append(art.summary(article))
        else:
            index[pos] = art.summary(article)
        saved.append(article)
        if before and before.get("published") and (not article["published"] or before["slug"] != article["slug"]):
            removed_public.append(before["slug"])
            if article["published"]:
                redirects = cat.add_redirect(redirects, f"/blog/{before['slug']}", f"/blog/{article['slug']}")

    writes = []  # (bucket, key, payload, cache, etag_name)
    if programs != old_programs:
        writes.append((cfg["public"], KEYS["courses"], dump(programs, False), PUBLIC_CACHE, "courses"))
    if site != old_site:
        writes.append((cfg["public"], KEYS["site"], dump(site, False), PUBLIC_CACHE, "site"))
    if changes:
        writes.append((cfg["private"], KEYS["article_index"], dump(index), None, "article_index"))

    # ---- refuse before writing anything if someone else changed these files since the pull
    checks = [(b, k, n) for b, k, _, _, n in writes]
    if redirects != (state["data"]["redirects"] or []):
        checks.append((cfg["public"], KEYS["redirects"], "redirects"))
    for bucket, key, name in checks:
        if r2.etag(bucket, key) != state["etags"].get(name):
            sys.exit(f"{key} was changed on the server since your last pull (probably in the admin). "
                     "Nothing was written. Run `pull --force`, redo the edit, and push again.")

    # ---- backup of the current live versions (in R2, so it survives cloud sessions, and locally)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = {"courses.json": old_programs, "site.json": old_site, "redirects.json": state["data"]["redirects"] or []}
    for _, _, _, before in changes:
        if before:
            backup[f"articles/{before['id']}.json"] = before
    local_dir = LOCAL_BACKUPS / stamp
    for name, data in backup.items():
        r2.put(cfg["private"], f"backups/{stamp}/{name}", dump(data).encode(), JSON_TYPE)
        (local_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (local_dir / name).write_text(dump(data), encoding="utf-8")

    # ---- write, in the same order as the admin
    try:
        for bucket, key, payload, cache, name in writes:
            etag = state["etags"].get(name)
            state["etags"][name] = r2.put(bucket, key, payload.encode(), JSON_TYPE, cache, if_match=etag, if_absent=not etag)
            print(f"  wrote {key}")
        for article in saved:
            r2.put(cfg["private"], KEYS["article"].format(id=article["id"]), dump(article).encode(), JSON_TYPE)
            if article["published"]:
                r2.put(cfg["public"], KEYS["public_article"].format(slug=article["slug"]),
                       dump(art.public_article(article), False).encode(), JSON_TYPE, PUBLIC_CACHE)
            print(f"  saved article {article['id']} {article['slug']}{' (live)' if article['published'] else ' (draft)'}")
        for slug in removed_public:
            r2.delete(cfg["public"], KEYS["public_article"].format(slug=slug))
        for aid in removed_private:
            r2.delete(cfg["private"], KEYS["article"].format(id=aid))
            print(f"  deleted article {aid}")
        if changes:
            r2.put(cfg["public"], KEYS["public_article_index"], dump(art.public_index(index), False).encode(), JSON_TYPE, PUBLIC_CACHE)
        if redirects != (state["data"]["redirects"] or []):
            etag = state["etags"].get("redirects")
            state["etags"]["redirects"] = r2.put(cfg["public"], KEYS["redirects"], dump(redirects).encode(), JSON_TYPE,
                                                 PUBLIC_CACHE, if_match=etag, if_absent=not etag)
            print(f"  wrote {KEYS['redirects']}")
    except R2Error as err:
        sys.exit(f"{err}\nThe push stopped part-way. The previous version is in backup {stamp} "
                 f"(`python3 aip.py restore {stamp}`). Run `pull --force` to see the current state.")

    # ---- local copy now matches the server
    state["data"].update(courses=programs, site=site, redirects=redirects, article_index=index)
    for aid in removed_private:
        state["articles"].pop(aid, None)
    for article in saved:
        state["articles"][article["id"]] = article
    for kind, name, _, _ in changes:
        if kind == "new":
            (ARTICLES / name).unlink(missing_ok=True)
    for article in saved:
        write_article_file(article)
    (CONTENT / "courses.json").write_text(dump(programs), encoding="utf-8")
    (CONTENT / "site.json").write_text(dump(site), encoding="utf-8")
    save_state(state)
    print(f"Published. Live on {cfg['site_url']} within about a minute. Backup of the previous version: {stamp}")
    for article in saved:
        if article["published"]:
            print(f"  {cfg['site_url']}/blog/{article['slug']}")


def cmd_seo(args):
    cfg = config()
    if "--live" in args:
        print(f"Crawling {cfg['site_url']} (sitemap, robots.txt, every listed page)...")
        issues, checked, total = seo.audit_live(cfg["site_url"])
        seo.print_report(issues, f"Live site, {checked} of {total} pages checked")
    else:
        programs, site, local_articles, _ = load_local()
        issues = seo.audit_content(programs, site["projects"], local_articles, cfg["site_url"])
        seo.print_report(issues, "Content")
        print("\nTip: `python3 aip.py seo --live` also checks the rendered pages, sitemap and robots.txt.")


def cmd_upload_image(args):
    if not args or not Path(args[0]).is_file():
        sys.exit("Usage: python3 aip.py upload-image path/to/image.png")
    cfg = config()
    path = Path(args[0])
    ctype = mimetypes.guess_type(path.name)[0] or ""
    if not ctype.startswith("image/"):
        sys.exit(f"{path.name} is not an image.")
    if path.stat().st_size > 5 * 1024 * 1024:
        sys.exit("Images must be under 5 MB. Compress it first.")
    key = f"images/{time.gmtime().tm_year}/{int(time.time() * 1000)}-{cat.to_slug(path.stem)[:60] or 'file'}{path.suffix.lower()}"
    client(cfg).put(cfg["public"], key, path.read_bytes(), ctype, "public, max-age=31536000, immutable")
    print(f"{cfg['public_url']}/{key}")


def cmd_backups(_args):
    cfg = config()
    names = client(cfg).list_folders(cfg["private"], "backups/")
    print("\n".join(names[-30:]) if names else "No backups yet.")


def cmd_restore(args):
    if not args:
        sys.exit("Usage: python3 aip.py restore NAME   (see `python3 aip.py backups`)")
    cfg = config()
    r2 = client(cfg)
    current_ids = set(load_state()["articles"])
    stamp = args[0]
    for name in ("courses.json", "site.json"):
        data, _ = r2.get_json(cfg["private"], f"backups/{stamp}/{name}")
        if data is None:
            sys.exit(f"Backup {stamp} not found (see `python3 aip.py backups`).")
        (CONTENT / name).write_text(dump(data), encoding="utf-8")
    restored = 0
    for key in r2.list_keys(cfg["private"], f"backups/{stamp}/articles/"):
        article, _ = r2.get_json(cfg["private"], key)
        if not article:
            continue
        if int(article["id"]) in current_ids:
            write_article_file(article)
        else:  # deleted since: recreate it as a new article
            article = {k: v for k, v in article.items() if k != "id"}
            (ARTICLES / f"restored-{article.get('slug')}.html").write_text(art.render_file(article), encoding="utf-8")
        restored += 1
    print(f"Restored backup {stamp} into content/ (courses, site{f', {restored} article(s)' if restored else ''}).")
    print("Run `python3 aip.py push` to review, then `push --yes` to publish it.")


COMMANDS = {"pull": cmd_pull, "list": cmd_list, "validate": cmd_validate, "diff": cmd_diff, "push": cmd_push,
            "seo": cmd_seo, "upload-image": cmd_upload_image, "backups": cmd_backups, "restore": cmd_restore}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    try:
        result = COMMANDS[sys.argv[1]](sys.argv[2:])
    except R2Error as err:
        sys.exit(str(err))
    if sys.argv[1] == "validate" and result is False:
        sys.exit(1)
