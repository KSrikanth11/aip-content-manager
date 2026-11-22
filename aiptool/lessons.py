"""Course lessons. One local file per lesson:

    content/lessons/<course-slug>/<NN>-<lesson-slug>.html

    ---
    title: Introduction to Generative AI and LLMs
    summary: One or two sentences shown under the title and in Google.
    minutes: 15
    credit: Adapted from Microsoft's "Generative AI for Beginners" (MIT License).
    creditUrl: https://github.com/microsoft/generative-ai-for-beginners
    ---
    <p>Body HTML...</p>

NN is the lesson number (its order); the rest of the file name is its URL slug:
/courses/<course-slug>/lessons/<lesson-slug>. Lessons are public as soon as they are pushed
(the course page links to them). The body is sanitized like an article, but keeps data-*
attributes, which drive quizzes, runnable exercises and the animated widgets.

Public bucket layout (read by the site):
    content/lessons/index.json                  { "<course>": [{slug, n, title, summary, minutes, updatedAt}] }
    content/lessons/<course>/<lesson>.json      the full lesson
"""
import re
from pathlib import Path

from . import articles as art
from .catalog import now_iso

FIELDS = ("title", "summary", "minutes", "credit", "creditUrl")
INDEX_FIELDS = ("slug", "n", "title", "summary", "minutes", "updatedAt")
NAME_RE = re.compile(r"^(\d{1,3})-([a-z0-9]+(?:-[a-z0-9]+)*)\.html$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def parse_file(path):
    path = Path(path)
    course = path.parent.name
    m = NAME_RE.match(path.name)
    if not m:
        raise ValueError(f"lessons/{course}/{path.name}: name must look like 01-lesson-slug.html (number, dash, lowercase-with-dashes)")
    raw = path.read_text(encoding="utf-8")
    h = re.match(r"\A\s*---\s*\n(.*?)\n---\s*\n?(.*)\Z", raw, re.S)
    if not h:
        raise ValueError(f"lessons/{course}/{path.name}: must start with a --- header block")
    meta = {}
    for line in h.group(1).splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"lessons/{course}/{path.name}: header line '{line}' needs 'name: value'")
        key, value = (s.strip() for s in line.split(":", 1))
        if key not in FIELDS:
            raise ValueError(f"lessons/{course}/{path.name}: unknown header '{key}' (allowed: {', '.join(FIELDS)})")
        meta[key] = value
    minutes = meta.get("minutes", "")
    if minutes and not minutes.isdigit():
        raise ValueError(f"lessons/{course}/{path.name}: minutes must be a whole number")
    return {
        "course": course,
        "n": int(m.group(1)),
        "slug": m.group(2),
        "title": meta.get("title", ""),
        "summary": meta.get("summary") or None,
        "minutes": int(minutes) if minutes else None,
        "credit": meta.get("credit") or None,
        "creditUrl": meta.get("creditUrl") or None,
        "content": h.group(2).strip(),
    }


def file_name(lesson):
    return f"{int(lesson['n']):02d}-{lesson['slug']}.html"


def render_file(lesson):
    head = [f"{k}: {'' if lesson.get(k) is None else lesson.get(k)}" for k in FIELDS]
    return "---\n" + "\n".join(head) + "\n---\n" + (lesson.get("content") or "") + "\n"


def load_dir(root):
    """[(course, slug) -> lesson], errors from content/lessons/."""
    found, errors = {}, []
    root = Path(root)
    if not root.exists():
        return found, errors
    for course_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if not SLUG_RE.match(course_dir.name):
            errors.append(f"lessons/{course_dir.name}: folder name must be the course slug (lowercase-with-dashes)")
            continue
        for path in sorted(course_dir.glob("*.html")):
            try:
                lesson = parse_file(path)
            except ValueError as err:
                errors.append(str(err))
                continue
            key = (lesson["course"], lesson["slug"])
            if key in found:
                errors.append(f"lessons/{course_dir.name}: two files use the slug '{lesson['slug']}'")
            found[key] = lesson
    return found, errors


def comparable(lesson):
    return {k: (lesson.get(k) if k in ("n", "minutes") else art.text(lesson.get(k))) for k in ("n", "title", "summary", "minutes", "credit", "creditUrl", "content")}


def finalize(local, existing):
    """Full lesson objects to publish: sanitized, with updatedAt kept when nothing changed."""
    out = {}
    now = now_iso()
    for key, lesson in local.items():
        item = dict(lesson)
        item["content"] = art.sanitize(item["content"], allow_data=True)
        before = existing.get(key)
        item["updatedAt"] = before.get("updatedAt") if before and comparable(before) == comparable(item) else now
        out[key] = item
    return out


def changes(existing, local):
    """[(kind, key, lesson, before)] with kind new | changed | deleted (compares sanitized content)."""
    result = []
    for key, lesson in sorted(local.items(), key=lambda kv: (kv[0][0], kv[1]["n"])):
        clean = dict(lesson, content=art.sanitize(lesson["content"], allow_data=True))
        before = existing.get(key)
        if before is None:
            result.append(("new", key, lesson, None))
        elif comparable(before) != comparable(clean):
            result.append(("changed", key, lesson, before))
    for key, before in existing.items():
        if key not in local:
            result.append(("deleted", key, None, before))
    return result


def describe(changes_list):
    lines = []
    for kind, (course, slug), lesson, before in changes_list:
        url = f"/courses/{course}/lessons/{slug}"
        if kind == "new":
            lines.append(f"  + new lesson {lesson['n']:02d}: {lesson['title']}  -> {url}  (goes LIVE)")
        elif kind == "changed":
            clean = dict(lesson, content=art.sanitize(lesson["content"], allow_data=True))
            fields = sorted(k for k, v in comparable(clean).items() if comparable(before).get(k) != v)
            lines.append(f"  ~ lesson {lesson['n']:02d} {lesson['title']}: {', '.join(fields)}")
        else:
            lines.append(f"  - DELETE lesson {before.get('title')}  ({url} will return 404)")
    return lines


def build_index(lessons):
    index = {}
    for (course, _), lesson in lessons.items():
        index.setdefault(course, []).append({k: lesson.get(k) for k in INDEX_FIELDS})
    for course in index:
        index[course].sort(key=lambda x: x["n"])
    return dict(sorted(index.items()))


def public_lesson(lesson):
    return {k: lesson.get(k) for k in ("course", "slug", "n", "title", "summary", "minutes", "credit", "creditUrl", "content", "updatedAt")}


def validate(local, program_slugs):
    errors, warnings = [], []
    numbers = {}
    for (course, slug), lesson in local.items():
        where = f"lessons/{course}/{file_name(lesson)}"
        if course not in program_slugs:
            errors.append(f"{where}: no course or internship has the slug '{course}' (folder name must match it)")
        if not art.text(lesson.get("title")):
            errors.append(f"{where}: title is required")
        if not art.text(lesson.get("content")):
            errors.append(f"{where}: the lesson body is empty")
        if re.search(r"<h1[\s>]", lesson.get("content") or "", re.I):
            errors.append(f"{where}: don't use <h1> in the body (the title is the page's h1); use <h2>")
        if not lesson.get("summary"):
            warnings.append(f"{where}: no summary (Google will make one up)")
        elif not 70 <= len(lesson["summary"]) <= 170:
            warnings.append(f"{where}: summary is {len(lesson['summary'])} characters (aim for 70–160)")
        for q in re.findall(r'<div[^>]*class="[^"]*\bquiz__q\b[^"]*"[^>]*>', lesson.get("content") or ""):
            if "data-answer=" not in q:
                errors.append(f"{where}: a quiz question has no data-answer=\"N\" (the number of the right option)")
        for img in re.findall(r"<img\b[^>]*>", lesson.get("content") or "", re.I):
            if not re.search(r'\balt="[^"]+"', img):
                warnings.append(f"{where}: an image has no alt text")
        numbers.setdefault((course, lesson["n"]), []).append(slug)
    for (course, n), slugs in numbers.items():
        if len(slugs) > 1:
            errors.append(f"lessons/{course}: lesson number {n:02d} is used by {', '.join(slugs)}")
    return errors, warnings
