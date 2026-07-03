"""
FIFA.com news scraper for PulseLineDaily.

Fetches official FIFA World Cup 2026 and general FIFA news from fifa.com.
Strategy:
  1. cloudscraper — bypasses Cloudflare JS challenges
  2. __NEXT_DATA__ JSON extraction (FIFA uses Next.js)
  3. JSON-LD structured data on article pages
  4. BeautifulSoup HTML fallback
All network errors are handled silently so the Celery task never crashes.
"""
import html as _html_module
import json
import logging
import re
import requests
from datetime import datetime, timedelta, timezone as _tz
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

BASE = 'https://www.fifa.com'
TIMEOUT = 25

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/126.0.0.0 Safari/537.36'
    ),
    'Accept': (
        'text/html,application/xhtml+xml,application/xml;'
        'q=0.9,image/avif,image/webp,*/*;q=0.8'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
    'Cache-Control': 'no-cache',
    'DNT': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Upgrade-Insecure-Requests': '1',
}

# FIFA pages to scrape for news articles
FIFA_NEWS_SOURCES = [
    {
        'url': 'https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/news',
        'label': 'FIFA World Cup 2026',
        'tags': 'fifa, world cup 2026, wc2026, football, sports, fifa news',
    },
    {
        'url': 'https://www.fifa.com/en/articles',
        'label': 'FIFA News',
        'tags': 'fifa, international football, football, sports, fifa news',
    },
]


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get_scraper():
    """Return a cloudscraper session, or a plain requests.Session as fallback."""
    try:
        import cloudscraper
        return cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
    except ImportError:
        logger.warning("cloudscraper not installed — falling back to requests (may be blocked by Cloudflare)")
        return requests.Session()


def _fetch_page(url, session=None):
    """
    Fetch a FIFA page. Returns HTML text or None on failure.
    Reuses a session if provided (avoids repeated Cloudflare handshakes).
    """
    sess = session or _get_scraper()
    try:
        resp = sess.get(url, headers=_HEADERS, timeout=TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        if len(resp.text) < 500:
            logger.warning("FIFA fetch returned near-empty page [%s] — likely JS-only render", url)
            return None
        return resp.text
    except Exception as exc:
        logger.warning("FIFA page fetch failed [%s]: %s", url, exc)
        return None


# ── JSON extractors ───────────────────────────────────────────────────────────

def _extract_next_data(html):
    """Extract __NEXT_DATA__ JSON embedded by Next.js. Returns dict or None."""
    m = re.search(r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception as exc:
        logger.debug("__NEXT_DATA__ JSON parse error: %s", exc)
        return None


def _extract_json_ld(html):
    """Return list of parsed JSON-LD objects found on the page."""
    results = []
    for m in re.finditer(
        r'<script\s+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL
    ):
        try:
            results.append(json.loads(m.group(1)))
        except Exception:
            pass
    return results


def _strip_html(raw):
    text = _html_module.unescape(raw or '')
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


# ── Article listing parsers ───────────────────────────────────────────────────

def _articles_from_next_data(next_data, source_label, max_articles):
    """
    Walk FIFA's __NEXT_DATA__ tree to find article items.
    Returns list of article dicts.
    """
    props = next_data.get('props', {}).get('pageProps', {})

    # Collect candidate lists from well-known keys FIFA uses
    candidates = []
    search_keys = ('articles', 'news', 'stories', 'items', 'data', 'results', 'content', 'list')

    def _search(obj, depth=0):
        if depth > 5 or candidates:
            return
        if isinstance(obj, list) and len(obj) >= 2:
            first = obj[0] if obj else {}
            if isinstance(first, dict) and (
                first.get('title') or first.get('headline') or first.get('slug')
            ):
                candidates.extend(obj)
                return
        if isinstance(obj, dict):
            for k in search_keys:
                v = obj.get(k)
                if isinstance(v, list) and v:
                    _search(v, depth + 1)
                    if candidates:
                        return
            for v in obj.values():
                _search(v, depth + 1)
                if candidates:
                    return

    _search(props)

    items = []
    for raw in candidates[:max_articles]:
        if not isinstance(raw, dict):
            continue

        title = (
            raw.get('title') or raw.get('headline') or
            raw.get('name') or raw.get('h1') or ''
        ).strip()
        if len(title) < 10:
            continue

        slug = raw.get('slug') or raw.get('url') or raw.get('link') or raw.get('path') or ''
        if not slug:
            continue
        url = slug if slug.startswith('http') else urljoin(BASE, slug)
        if 'fifa.com' not in url:
            continue

        excerpt = _strip_html(
            raw.get('description') or raw.get('abstract') or
            raw.get('summary') or raw.get('excerpt') or
            raw.get('body') or ''
        )[:1000]

        pub_raw = (
            raw.get('publishedDate') or raw.get('datePublished') or
            raw.get('date') or raw.get('created') or
            raw.get('published_at') or ''
        )
        pub_iso = _parse_date(pub_raw)

        items.append({
            'title': title,
            'url': url,
            'excerpt': excerpt,
            'published_iso': pub_iso,
            'source_label': source_label,
        })

    return items


def _articles_from_json_ld(html, source_label):
    """Extract article links from JSON-LD ItemList on a listing page."""
    items = []
    for ld in _extract_json_ld(html):
        if not isinstance(ld, dict):
            continue
        if ld.get('@type') == 'ItemList':
            for element in (ld.get('itemListElement') or []):
                obj = element.get('item', element) if isinstance(element, dict) else {}
                title = (obj.get('name') or obj.get('headline') or '').strip()
                url = obj.get('url') or ''
                if title and url and len(title) >= 10 and 'fifa.com' in url:
                    items.append({
                        'title': title,
                        'url': url,
                        'excerpt': _strip_html(obj.get('description') or '')[:1000],
                        'published_iso': _parse_date(obj.get('datePublished') or ''),
                        'source_label': source_label,
                    })
    return items


def _articles_from_html(html, source_label, base_url):
    """Last-resort BeautifulSoup scrape of a FIFA listing page."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.error("beautifulsoup4 not installed")
        return []

    try:
        soup = BeautifulSoup(html, 'lxml')
    except Exception:
        soup = BeautifulSoup(html, 'html.parser')

    items = []

    # Strategy A: <article> tags
    for art in soup.find_all('article', limit=20):
        link = art.find('a', href=True)
        heading = art.find(['h1', 'h2', 'h3', 'h4'])
        if not link or not heading:
            continue
        title = heading.get_text(strip=True)
        if len(title) < 10:
            continue
        href = link.get('href', '')
        if not href or href.startswith('#') or href.startswith('javascript'):
            continue
        url = href if href.startswith('http') else urljoin(base_url, href)
        if 'fifa.com' not in url:
            continue
        p = art.find('p')
        items.append({
            'title': title,
            'url': url,
            'excerpt': p.get_text(strip=True)[:1000] if p else '',
            'published_iso': None,
            'source_label': source_label,
        })
        if len(items) >= 15:
            break

    if items:
        return items

    # Strategy B: links containing /en/articles/ or /en/tournaments/
    seen = set()
    for a in soup.find_all('a', href=True):
        href = a.get('href', '')
        if '/en/articles/' not in href and '/en/tournaments/' not in href:
            continue
        url = href if href.startswith('http') else urljoin(BASE, href)
        if url in seen or 'fifa.com' not in url:
            continue
        seen.add(url)
        # find nearby heading or use link text
        heading_tag = a.find(['h1', 'h2', 'h3', 'h4']) or a.find_parent(['h1', 'h2', 'h3', 'h4'])
        title = (heading_tag.get_text(strip=True) if heading_tag else a.get_text(strip=True)).strip()
        if len(title) < 10:
            continue
        items.append({
            'title': title,
            'url': url,
            'excerpt': '',
            'published_iso': None,
            'source_label': source_label,
        })
        if len(items) >= 15:
            break

    return items


# ── Article body fetcher ──────────────────────────────────────────────────────

def fetch_fifa_article_body(url, session=None, max_chars=3000):
    """
    Fetch the readable body of a FIFA article page.
    Tries JSON-LD articleBody, then __NEXT_DATA__, then BeautifulSoup.
    Returns clean text (up to max_chars) or empty string on failure.
    """
    html = _fetch_page(url, session=session)
    if not html:
        return ''

    # 1. JSON-LD — most reliable when present
    for ld in _extract_json_ld(html):
        if not isinstance(ld, dict):
            continue
        if ld.get('@type') in ('NewsArticle', 'Article', 'Report'):
            body = ld.get('articleBody') or ld.get('description') or ''
            if len(body) > 200:
                return _strip_html(body)[:max_chars]

    # 2. __NEXT_DATA__
    next_data = _extract_next_data(html)
    if next_data:
        props = next_data.get('props', {}).get('pageProps', {})
        for key in ('article', 'story', 'content', 'data', 'page'):
            obj = props.get(key)
            if isinstance(obj, dict):
                body = (
                    obj.get('body') or obj.get('articleBody') or
                    obj.get('content') or obj.get('bodyText') or
                    obj.get('description') or ''
                )
                if isinstance(body, str) and len(body) > 200:
                    return _strip_html(body)[:max_chars]
                # Some CMSes return body as a list of block objects
                if isinstance(body, list):
                    texts = []
                    for block in body:
                        if isinstance(block, dict):
                            texts.append(block.get('text') or block.get('value') or '')
                        elif isinstance(block, str):
                            texts.append(block)
                    joined = ' '.join(t for t in texts if t)
                    if len(joined) > 200:
                        return _strip_html(joined)[:max_chars]

    # 3. BeautifulSoup
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'lxml')
    except ImportError:
        return ''
    except Exception:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')

    for noise in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe']):
        noise.decompose()

    # Try common article content containers
    container = (
        soup.find('article') or
        soup.find(attrs={'data-testid': re.compile(r'article|story|content', re.I)}) or
        soup.find('div', class_=re.compile(r'article.?(body|content|text)|story.?body', re.I)) or
        soup.find('main')
    )
    if container:
        paras = [p.get_text(strip=True) for p in container.find_all('p') if len(p.get_text(strip=True)) > 30]
        if paras:
            return ' '.join(paras)[:max_chars]

    return ''


# ── Date helpers ──────────────────────────────────────────────────────────────

def _parse_date(raw):
    """Parse a date string into ISO format, or return None."""
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        return dt.isoformat()
    except Exception:
        pass
    # Try common formats
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d/%m/%Y'):
        try:
            dt = datetime.strptime(raw[:len(fmt)], fmt)
            return dt.replace(tzinfo=_tz.utc).isoformat()
        except Exception:
            pass
    return None


# ── Main scraper ──────────────────────────────────────────────────────────────

def scrape_fifa_source(source, session=None, max_articles=12, max_age_days=3):
    """
    Scrape a FIFA news listing page and return article dicts.
    Each dict: {title, url, excerpt, published_iso, source_label}.
    """
    url = source['url']
    label = source['label']

    html = _fetch_page(url, session=session)
    if not html:
        logger.warning("FIFA scraper [%s] — could not fetch listing page", label)
        return []

    items = []

    # Strategy 1: __NEXT_DATA__ JSON (most reliable for Next.js)
    next_data = _extract_next_data(html)
    if next_data:
        items = _articles_from_next_data(next_data, label, max_articles)
        if items:
            logger.info("FIFA scraper [%s] — %d articles via __NEXT_DATA__", label, len(items))

    # Strategy 2: JSON-LD ItemList
    if not items:
        items = _articles_from_json_ld(html, label)
        if items:
            logger.info("FIFA scraper [%s] — %d articles via JSON-LD", label, len(items))

    # Strategy 3: BeautifulSoup HTML
    if not items:
        items = _articles_from_html(html, label, url)
        if items:
            logger.info("FIFA scraper [%s] — %d articles via HTML fallback", label, len(items))

    if not items:
        logger.warning("FIFA scraper [%s] — no articles found (page may be JS-only)", label)
        return []

    # Age filter
    if max_age_days:
        cutoff = datetime.now(_tz.utc) - timedelta(days=max_age_days)
        kept = []
        for item in items:
            pub = item.get('published_iso')
            if pub:
                try:
                    pub_dt = datetime.fromisoformat(pub.replace('Z', '+00:00'))
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=_tz.utc)
                    if pub_dt < cutoff:
                        logger.debug("FIFA scraper — skipping old article: %s", item['title'][:60])
                        continue
                except Exception:
                    pass
            kept.append(item)
        items = kept

    return items[:max_articles]
