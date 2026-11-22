"""SEO checks: `content` audits the pulled files, `live` crawls the real site via its sitemap."""
import concurrent.futures
import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

USER_AGENT = "Mozilla/5.0 (compatible; aip-seo-audit/1.0)"
TITLE_MAX, DESC_MIN, DESC_MAX, THIN_WORDS = 60, 70, 160, 300


def plain(markup, limit=None):
    out = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup or "")).strip()
    return out[:limit] if limit else out


# ------------------------------------------------------------------ content audit (offline)

def audit_content(programs, projects, articles, site_url):
    """Returns [(level, where, message)] with level 'error' | 'warn'."""
    issues = []
    add = lambda level, where, msg: issues.append((level, where, msg))
    host = re.sub(r"^https?://", "", site_url).split("/")[0]

    titles = {}
    for p in programs:
        kind = "internship" if p.get("type") == "internship" else "course"
        where = f"{kind} {p.get('id')} {p.get('title')}"
        suffix = " – Remote Internship with Certificate" if kind == "internship" else " – Course with Certificate"
        title = p.get("seoTitle") or f"{p.get('title')}{suffix}"
        desc = p.get("seoDescription") or plain(p.get("hook") or p.get("description"), 158)
        _check_item(add, where, title, desc, p)
        titles.setdefault(title.lower(), []).append(where)
    for p in projects:
        where = f"project {p.get('id')} {p.get('title')}"
        title = p.get("seoTitle") or f"{p.get('title')} – Project with Source Code"
        desc = p.get("seoDescription") or plain(p.get("hook") or p.get("description"), 158)
        _check_item(add, where, title, desc, p)
        if not p.get("slug"):
            add("warn", where, "no slug, so the URL is /projects/<number>. A readable slug ranks better (a redirect is added automatically).")
        titles.setdefault(title.lower(), []).append(where)

    descs = {}
    for name, a in articles:
        if not a.get("published"):
            continue
        where = f"article {name}"
        title, desc, body = a.get("title") or "", a.get("metaDescription") or "", a.get("content") or ""
        words = len(plain(body).split())
        if len(title) > TITLE_MAX:
            add("warn", where, f"title is {len(title)} characters; Google shows about {TITLE_MAX}.")
        if not desc:
            add("error", where, "missing metaDescription.")
        elif not DESC_MIN <= len(desc) <= DESC_MAX:
            add("warn", where, f"metaDescription is {len(desc)} characters (aim for {DESC_MIN}–{DESC_MAX}).")
        if words < THIN_WORDS:
            add("warn", where, f"only {words} words. Thin pages rarely rank; aim for 800+ for guides.")
        if re.search(r"<h1[\s>]", body, re.I):
            add("error", where, "content has an <h1>. The page already uses the title as <h1>; use <h2>/<h3>.")
        if words >= THIN_WORDS and not re.search(r"<h2[\s>]", body, re.I):
            add("warn", where, "no <h2> subheadings. Break long text into sections.")
        for img in re.findall(r"<img\b[^>]*>", body, re.I):
            if not re.search(r'\balt\s*=\s*"[^"]+"', img, re.I):
                add("warn", where, "an image has no alt text.")
                break
        links = re.findall(r'href\s*=\s*"([^"]+)"', body, re.I)
        if not any(l.startswith("/") or host in l for l in links):
            add("warn", where, "no internal links. Link to a related course, internship or article.")
        if not a.get("featuredImage"):
            add("warn", where, "no featuredImage (used for link previews on WhatsApp, LinkedIn, Google Discover).")
        if not a.get("category"):
            add("warn", where, "no category.")
        if desc:
            descs.setdefault(desc.lower(), []).append(where)
        titles.setdefault(title.lower(), []).append(where)

    for t, where in titles.items():
        if len(where) > 1:
            add("error", ", ".join(where), f"same page title used {len(where)} times: '{t}'.")
    for d, where in descs.items():
        if len(where) > 1:
            add("warn", ", ".join(where), "same meta description used more than once.")
    return issues


def _check_item(add, where, title, desc, p):
    if len(title) > TITLE_MAX:
        add("warn", where, f"page title is {len(title)} characters ('{title}'); set a shorter seoTitle (≤ {TITLE_MAX}).")
    if len(desc) < DESC_MIN:
        add("warn", where, f"meta description is only {len(desc)} characters; add an seoDescription ({DESC_MIN}–{DESC_MAX}).")
    if p.get("seoDescription") and len(p["seoDescription"]) > DESC_MAX:
        add("warn", where, f"seoDescription is {len(p['seoDescription'])} characters; Google cuts it at about {DESC_MAX}.")
    if not p.get("image"):
        add("warn", where, "no image (cards and link previews look empty).")
    if not p.get("description") or len(plain(p.get("description")).split()) < 60:
        add("warn", where, "short description (under 60 words). More detail helps both ranking and conversions.")


# ------------------------------------------------------------------ live audit

class _Head(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.in_title = "", False
        self.meta, self.canonical, self.h1, self.jsonld, self.imgs_no_alt = {}, None, 0, 0, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key:
                self.meta[key] = a.get("content") or ""
        elif tag == "link" and (a.get("rel") or "").lower() == "canonical":
            self.canonical = a.get("href")
        elif tag == "h1":
            self.h1 += 1
        elif tag == "script" and a.get("type") == "application/ld+json":
            self.jsonld += 1
        elif tag == "img" and a.get("alt") is None and a.get("aria-hidden") != "true":  # alt="" is fine (decorative)
            self.imgs_no_alt += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    start = time.time()
    try:
        with _opener.open(req, timeout=timeout) as res:
            return res.status, res.headers, res.read().decode("utf-8", "replace"), time.time() - start
    except urllib.error.HTTPError as err:
        return err.code, err.headers, "", time.time() - start
    except (urllib.error.URLError, TimeoutError) as err:
        return 0, {}, str(getattr(err, "reason", err)), time.time() - start


def _check_page(url):
    issues = []
    status, headers, body, secs = fetch(url)
    if status in (301, 302, 307, 308):
        return [("error", url, f"in the sitemap but redirects ({status}) to {headers.get('Location')}.")]
    if status != 200:
        return [("error", url, f"returns {status or 'no response: ' + body}.")]
    page = _Head()
    page.feed(body)
    title, desc = page.title.strip(), page.meta.get("description", "")
    if not title:
        issues.append(("error", url, "no <title>."))
    elif len(title) > 70:
        issues.append(("warn", url, f"title is {len(title)} characters: '{title}'."))
    if not desc:
        issues.append(("error", url, "no meta description."))
    elif not DESC_MIN <= len(desc) <= DESC_MAX + 10:
        issues.append(("warn", url, f"meta description is {len(desc)} characters."))
    if "noindex" in page.meta.get("robots", "").lower():
        issues.append(("error", url, "has noindex but is in the sitemap."))
    if page.canonical and page.canonical.rstrip("/") != url.rstrip("/"):
        issues.append(("warn", url, f"canonical points elsewhere: {page.canonical}"))
    if not page.canonical:
        issues.append(("warn", url, "no canonical link."))
    if page.h1 != 1:
        issues.append(("warn", url, f"has {page.h1} <h1> tags (should be exactly 1)."))
    if not page.meta.get("og:image"):
        issues.append(("warn", url, "no og:image (link previews have no picture)."))
    if not page.jsonld:
        issues.append(("warn", url, "no JSON-LD structured data."))
    if page.imgs_no_alt:
        issues.append(("warn", url, f"{page.imgs_no_alt} image(s) without alt text."))
    if secs > 2.5:
        issues.append(("warn", url, f"slow response: {secs:.1f}s."))
    return issues


def audit_live(site_url, limit=150):
    issues = []
    site_url = site_url.rstrip("/")
    status, _, robots, _ = fetch(f"{site_url}/robots.txt")
    if status != 200:
        issues.append(("error", "/robots.txt", f"returns {status}."))
    else:
        if re.search(r"^\s*Disallow:\s*/\s*$", robots, re.M | re.I):
            issues.append(("error", "/robots.txt", "blocks the whole site (Disallow: /)."))
        if "sitemap:" not in robots.lower():
            issues.append(("warn", "/robots.txt", "doesn't list the sitemap."))
    status, _, sitemap, _ = fetch(f"{site_url}/sitemap.xml")
    if status != 200:
        return issues + [("error", "/sitemap.xml", f"returns {status or sitemap}. Can't crawl further.")], 0, 0
    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sitemap)
    if not urls:
        return issues + [("error", "/sitemap.xml", "lists no URLs.")], 0, 0
    other = {u for u in urls if not u.startswith(site_url)}
    if other:
        issues.append(("error", "/sitemap.xml", f"{len(other)} URL(s) on another domain, e.g. {sorted(other)[0]}."))
    status, _, _, _ = fetch(f"{site_url}/this-page-should-not-exist-{int(time.time())}")
    if status != 404:
        issues.append(("error", "404 handling", f"a missing page returns {status} instead of 404 (soft 404)."))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for found in pool.map(_check_page, urls[:limit]):
            issues += found
    return issues, len(urls[:limit]), len(urls)


def print_report(issues, header):
    errors = [i for i in issues if i[0] == "error"]
    warns = [i for i in issues if i[0] == "warn"]
    print(f"\n{header}: {len(errors)} error(s), {len(warns)} warning(s)")
    for level, where, msg in errors + warns:
        print(f"  {'ERROR' if level == 'error' else 'warn '}  {where}: {msg}")
