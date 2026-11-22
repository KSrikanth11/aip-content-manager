"""Blog articles. Each one is a local file content/articles/<id>-<slug>.html:

    ---
    id: 3                      (leave out for a new article)
    title: How to build a RAG chatbot
    slug: rag-chatbot-guide    (optional; made from the title when empty)
    published: false           (true = live at /blog/<slug>)
    metaDescription: ...
    category: Generative AI
    tags: RAG, LLM, Python
    featuredImage: https://media.aiprojects4u.com/images/...
    ---
    <h2>...</h2><p>...</p>

Same rules as the admin: slugs, tags, sanitized HTML, drafts private, published copies public.
"""
import html
import re
from html.parser import HTMLParser
from pathlib import Path

from .catalog import now_iso

FIELDS = ("id", "title", "slug", "published", "metaDescription", "category", "tags", "featuredImage")
SUMMARY_FIELDS = ("id", "title", "slug", "metaDescription", "category", "tags", "featuredImage", "createdAt", "updatedAt", "published")


# ------------------------------------------------------------------ files

def parse_file(path):
    raw = Path(path).read_text(encoding="utf-8")
    m = re.match(r"\A\s*---\s*\n(.*?)\n---\s*\n?(.*)\Z", raw, re.S)
    if not m:
        raise ValueError(f"{Path(path).name}: must start with a --- header block (see an existing article)")
    meta = {}
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"{Path(path).name}: header line '{line}' needs 'name: value'")
        key, value = (s.strip() for s in line.split(":", 1))
        if key not in FIELDS:
            raise ValueError(f"{Path(path).name}: unknown header '{key}' (allowed: {', '.join(FIELDS)})")
        meta[key] = value
    if meta.get("id"):
        if not meta["id"].isdigit():
            raise ValueError(f"{Path(path).name}: id must be a number")
        meta["id"] = int(meta["id"])
    else:
        meta.pop("id", None)
    pub = meta.get("published", "false").lower()
    if pub not in ("true", "false", "yes", "no"):
        raise ValueError(f"{Path(path).name}: published must be true or false")
    meta["published"] = pub in ("true", "yes")
    meta["content"] = m.group(2).strip()
    return meta


def render_file(article):
    head = [f"{k}: {'true' if article.get(k) else 'false'}" if k == "published" else f"{k}: {article.get(k) or ''}" for k in FIELDS]
    return "---\n" + "\n".join(head) + "\n---\n" + (article.get("content") or "") + "\n"


def file_name(article):
    return f"{article['id']}-{article['slug']}.html"


# ------------------------------------------------------------------ admin rules

def text(v):
    return "" if v is None else str(v).strip()


def normalize_slug(v):
    return re.sub(r"^-|-$", "", re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", text(v).lower())))


def to_slug(title):
    return re.sub(r"^-|-$", "", re.sub(r"[^a-z0-9]+", "-", text(title).lower()))


def normalize_tags(raw):
    seen = []
    for t in re.split(r"[,\n]", str(raw or "")):
        t = t.strip()
        if t and t not in seen:
            seen.append(t)
    return ", ".join(seen) or None


def unique_slug(index, base, current_id):
    base = normalize_slug(base) or "article"
    candidate, n = base, 2
    while any(a.get("slug") == candidate and a.get("id") != current_id for a in index):
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def apply(index, existing, payload):
    """New version of an article from a local file (mirrors the admin's applyPayload)."""
    now = now_iso()
    article = dict(existing) if existing else {
        "id": max([int(a.get("id") or 0) for a in index] + [0]) + 1,
        "createdAt": now,
    }
    article["title"] = text(payload.get("title"))
    article["content"] = sanitize(text(payload.get("content")))
    article["metaDescription"] = text(payload.get("metaDescription")) or None
    article["category"] = text(payload.get("category")) or None
    article["tags"] = normalize_tags(payload.get("tags"))
    article["featuredImage"] = text(payload.get("featuredImage")) or None
    article["published"] = bool(payload.get("published"))
    article["updatedAt"] = now
    requested = normalize_slug(payload["slug"]) if text(payload.get("slug")) else to_slug(article["title"])
    article["slug"] = unique_slug(index, requested, article["id"] if existing else None)
    return article


def summary(article):
    return {f: article.get(f) for f in SUMMARY_FIELDS}


def public_index(index):
    live = [a for a in index if a and a.get("published") and a.get("slug")]
    live.sort(key=lambda a: a.get("createdAt") or "", reverse=True)
    return [{k: v for k, v in summary(a).items() if k != "published"} for a in live]


def public_article(article):
    return {k: v for k, v in article.items() if k != "published"}


def comparable(article):
    """The parts a local file controls, for change detection."""
    out = {k: text(article.get(k)) for k in FIELDS if k not in ("id", "published")}
    out.update(content=text(article.get("content")), tags=normalize_tags(article.get("tags")) or "",
               published=bool(article.get("published")))
    return out


def validate(local):
    errors, warnings = [], []
    for name, a in local:
        if not text(a.get("title")):
            errors.append(f"article {name}: title is required")
        if not text(a.get("content")):
            errors.append(f"article {name}: content is required")
        if a.get("published") and not text(a.get("metaDescription")):
            warnings.append(f"article {name}: no metaDescription (Google will make one up)")
    ids = [a["id"] for _, a in local if a.get("id")]
    for i in {i for i in ids if ids.count(i) > 1}:
        errors.append(f"two article files have id {i}")
    return errors, warnings


# ------------------------------------------------------------------ HTML sanitizer (same allowlist as the admin)

ALLOWED_TAGS = {"a", "b", "blockquote", "br", "code", "div", "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6",
                "hr", "i", "iframe", "img", "li", "ol", "p", "pre", "s", "small", "source", "span", "strong", "sub", "sup",
                "table", "tbody", "td", "th", "thead", "tr", "u", "ul", "video"}
DROP_WITH_CONTENT = {"script", "style", "object", "embed", "form", "input", "button", "textarea", "select", "link", "meta",
                     "base", "noscript", "template"}
VOID = {"br", "hr", "img", "source", "input", "link", "meta", "base"}
ALLOWED_ATTRS = {"a": {"href", "target", "rel"}, "img": {"src", "alt", "width", "height", "loading"},
                 "iframe": {"src", "width", "height", "allow", "allowfullscreen", "frameborder"},
                 "video": {"src", "controls", "poster", "width", "height", "preload"}, "source": {"src", "type"},
                 "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan"}}
COMMON_ATTRS = {"class", "id", "title"}
URL_ATTRS = {"href", "src", "poster"}
IFRAME_HOSTS = {"www.youtube.com", "www.youtube-nocookie.com", "player.vimeo.com"}


def _safe_url(value, iframe=False):
    raw = (value or "").strip()
    if not raw:
        return ""
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", raw)
    if iframe:
        host = re.match(r"^https://([^/?#]+)", raw)
        return raw if host and host.group(1).lower() in IFRAME_HOSTS else ""
    if m and m.group(1).lower() not in ("http", "https", "mailto", "tel"):
        return ""
    return raw


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.stack = [], 0, []

    def handle_starttag(self, tag, attrs, self_closing=False):
        if self.skip:
            if tag in DROP_WITH_CONTENT and tag not in VOID and not self_closing:
                self.skip += 1
            return
        if tag in DROP_WITH_CONTENT:
            if tag not in VOID and not self_closing:
                self.skip = 1
            return
        if tag not in ALLOWED_TAGS:
            return  # drop the tag, keep its text
        allowed = COMMON_ATTRS | ALLOWED_ATTRS.get(tag, set())
        kept = {}
        for name, value in attrs:
            if name not in allowed:
                continue
            if name in URL_ATTRS:
                value = _safe_url(value, iframe=tag == "iframe")
                if not value:
                    continue
            kept[name] = value
        if tag == "iframe" and "src" not in kept:
            if not self_closing:
                self.skip = 1
                self.stack.append("#dropped-iframe")
            return
        if tag == "a" and kept.get("target") == "_blank":
            kept["rel"] = "noopener noreferrer"
        attr_text = "".join(f' {k}="{html.escape(v, quote=True)}"' if v is not None else f" {k}" for k, v in kept.items())
        self.out.append(f"<{tag}{attr_text}>")
        if tag not in VOID and not self_closing:
            self.stack.append(tag)
        elif self_closing and tag not in VOID:
            self.out.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs, self_closing=True)

    def handle_endtag(self, tag):
        if self.skip:
            if tag in DROP_WITH_CONTENT or (tag == "iframe" and self.stack and self.stack[-1] == "#dropped-iframe"):
                self.skip -= 1
                if self.stack and self.stack[-1] == "#dropped-iframe":
                    self.stack.pop()
            return
        if tag in self.stack:
            while self.stack:
                open_tag = self.stack.pop()
                self.out.append(f"</{open_tag}>")
                if open_tag == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def result(self):
        self.close()
        return "".join(self.out) + "".join(f"</{t}>" for t in reversed(self.stack) if not t.startswith("#"))


def sanitize(markup):
    if not re.search(r"[<>]", markup or ""):
        return markup or ""
    s = _Sanitizer()
    s.feed(markup)
    return s.result()
