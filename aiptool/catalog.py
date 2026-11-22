"""Courses, internships (content/courses.json) and projects (site.json → projects).
Same rules the admin dashboard applies on save."""
import datetime
import re
import urllib.parse

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LIST_FIELDS = ("features", "workflow", "outcomes", "deliverables")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def to_slug(title):
    return re.sub(r"^-|-$", "", re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()))


def unique_slug(items, slug, item_id):
    base = candidate = slug or "item"
    n = 2
    while any(i.get("slug") == candidate and i.get("id") != item_id for i in items):
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def program_path(p):
    section = "internships" if p.get("type") == "internship" else "courses"
    return f"/{section}/{urllib.parse.quote(str(p.get('slug') or p.get('id')))}"


def project_path(p):
    return f"/projects/{urllib.parse.quote(str(p.get('slug') or p.get('id')))}"


def add_redirect(rules, src, dst):
    rules = [r for r in rules if r and r.get("from") != src]
    rules = [{**r, "to": dst} if r.get("to") == src else r for r in rules]
    rules = [r for r in rules if r.get("from") != r.get("to")]
    if src != dst:
        rules.append({"from": src, "to": dst})
    return rules


def _next_id(*lists, floor=0):
    return max([int(x.get("id") or 0) for items in lists for x in items] + [floor]) + 1


def finalize(programs, projects, old_programs, old_projects):
    """Fill in ids, type, slugs and updatedAt like the admin does."""
    old_by_id = {p.get("id"): p for p in old_programs}
    strip = lambda p: {k: v for k, v in p.items() if k != "updatedAt"}
    for p in programs:
        if not p.get("id"):
            p["id"] = _next_id(programs, old_programs, floor=100)
        p["type"] = "course" if p.get("type") == "course" else "internship"
        p["slug"] = unique_slug(programs, p.get("slug") or to_slug(p.get("title")), p["id"])
        before = old_by_id.get(p["id"])
        if before is None or strip(before) != strip(p):
            p["updatedAt"] = now_iso()
    for p in projects:
        if not p.get("id"):
            p["id"] = _next_id(projects, old_projects)
        if p.get("slug"):  # projects keep their numeric URL (/projects/1) unless given a slug
            p["slug"] = unique_slug(projects, p["slug"], p["id"])


def _is_price(v):
    return v in (None, "") or (isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0)


def validate(programs, projects):
    errors, warnings = [], []

    def check(kind, items, path_of):
        ids, paths = set(), set()
        for idx, p in enumerate(items):
            if not isinstance(p, dict):
                errors.append(f"{kind} #{idx + 1}: must be an object")
                continue
            label = f"{kind} #{idx + 1} ({p.get('title') or 'untitled'})"
            if not str(p.get("title") or "").strip():
                errors.append(f"{label}: title is required")
            if p.get("id"):
                if p["id"] in ids:
                    errors.append(f"{label}: duplicate id {p['id']}")
                ids.add(p["id"])
            if p.get("slug"):
                if not SLUG_RE.match(p["slug"]):
                    errors.append(f"{label}: slug '{p['slug']}' must be lowercase letters, digits and single dashes")
                if path_of(p) in paths:
                    errors.append(f"{label}: URL {path_of(p)} is used twice")
                paths.add(path_of(p))
            for f in ("originalPrice", "discountPrice"):
                if not _is_price(p.get(f)):
                    errors.append(f"{label}: {f} must be a plain number >= 0 (no quotes, no ₹)")
            orig, disc = p.get("originalPrice") or 0, p.get("discountPrice") or 0
            if _is_price(orig) and _is_price(disc) and orig and disc > orig:
                errors.append(f"{label}: offer price must be lower than the original price")
            for f in LIST_FIELDS:
                if f in p and not isinstance(p[f], list):
                    errors.append(f"{label}: {f} must be a list of strings")
            if not p.get("hook"):
                warnings.append(f"{label}: no hook (the one-line summary on cards)")
            if not p.get("description"):
                warnings.append(f"{label}: no description")

    check("program", programs, program_path)
    for idx, p in enumerate(programs):
        if isinstance(p, dict):
            if p.get("type") not in (None, "", "internship", "course"):
                errors.append(f"program #{idx + 1}: type must be 'internship' or 'course'")
            if p.get("startDate") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(p["startDate"])):
                errors.append(f"program #{idx + 1}: startDate must look like 2026-12-15")
    check("project", projects, project_path)
    return errors, warnings


def describe_changes(kind, old, new, path_of):
    lines = []
    old_by_id = {p.get("id"): p for p in old}
    new_ids = {p.get("id") for p in new if p.get("id")}
    for p in new:
        before = old_by_id.get(p.get("id")) if p.get("id") else None
        if before is None:
            where = path_of(p) if p.get("id") else "(id and URL assigned on push)"
            lines.append(f"  + new {kind}: {p.get('title')}  ->  {where}")
            continue
        changed = sorted(k for k in set(before) | set(p) if k != "updatedAt" and before.get(k) != p.get(k))
        if changed:
            lines.append(f"  ~ {kind} {p.get('id')} {p.get('title')}: {', '.join(changed)}")
            if path_of(before) != path_of(p):
                lines.append(f"      URL {path_of(before)} -> {path_of(p)} (301 redirect added)")
            for k in changed:
                if k in ("originalPrice", "discountPrice"):
                    lines.append(f"      {k}: {before.get(k)} -> {p.get(k)}")
    for p in old:
        if p.get("id") not in new_ids:
            lines.append(f"  - DELETE {kind} {p.get('id')} {p.get('title')}  ({path_of(p)} will return 404)")
    kept_old = [p.get("id") for p in old if p.get("id") in new_ids]
    kept_new = [p.get("id") for p in new if p.get("id") in old_by_id]
    if kept_old != kept_new:
        lines.append(f"  ~ {kind} order changed")
    return lines


def redirects_for(old, new, path_of, rules):
    new_by_id = {p.get("id"): p for p in new}
    for before in old:
        after = new_by_id.get(before.get("id"))
        if after and path_of(before) != path_of(after):
            rules = add_redirect(rules, path_of(before), path_of(after))
    return rules
