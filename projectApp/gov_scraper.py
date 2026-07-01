import html as _html_module
import io
import logging
import os
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (compatible; PulseLineDaily-GovBot/1.0; '
        '+https://www.pulselinedaily.com)'
    )
}
TIMEOUT = 15

_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.tiff', '.ico'}


# ── URL helpers ───────────────────────────────────────────────────────────────

def _absolute(url, base):
    url = url.strip()
    if url.startswith('http'):
        return url
    if url.startswith('//'):
        return 'https:' + url
    base = base.rstrip('/')
    if not url.startswith('/'):
        url = '/' + url
    return base + url


def _url_extension(url):
    try:
        path = urlparse(url).path
        return os.path.splitext(path)[1].lower()
    except Exception:
        return ''


# ── Content fetchers ──────────────────────────────────────────────────────────

class _HtmlTextExtractor(HTMLParser):
    _SKIP = {'script', 'style', 'nav', 'header', 'footer', 'aside', 'noscript', 'iframe'}

    def __init__(self):
        super().__init__()
        self._depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self._SKIP:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in self._SKIP and self._depth > 0:
            self._depth -= 1

    def handle_data(self, data):
        if not self._depth:
            text = data.strip()
            if text:
                self.parts.append(text)


def _extract_html_text(html_text, max_chars):
    try:
        extractor = _HtmlTextExtractor()
        extractor.feed(_html_module.unescape(html_text))
        text = ' '.join(extractor.parts)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]
    except Exception as exc:
        logger.warning("HTML text extraction failed: %s", exc)
        return ''


def _extract_pdf_text(content_bytes, max_chars):
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(content_bytes)) as pdf:
            parts = []
            for page in pdf.pages[:8]:
                text = page.extract_text()
                if text:
                    parts.append(text)
        text = '\n'.join(parts)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]
    except ImportError:
        logger.warning("pdfplumber not installed — PDF text extraction skipped")
        return ''
    except Exception as exc:
        logger.warning("PDF text extraction failed: %s", exc)
        return ''


def fetch_gov_content(url, max_chars=2500):
    """
    Fetch readable text from a gov agency URL.
    Handles HTML pages, PDFs, and gracefully skips image URLs.
    Returns empty string on any failure — caller must handle that case.
    """
    ext = _url_extension(url)

    if ext in _IMAGE_EXTENSIONS:
        logger.info("Gov scraper — skipping image URL: %s", url)
        return ''

    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', '').lower()
    except Exception as exc:
        logger.warning("Gov content fetch failed (%s): %s", url, exc)
        return ''

    if 'application/pdf' in content_type or ext == '.pdf':
        return _extract_pdf_text(resp.content, max_chars)

    if 'image/' in content_type:
        logger.info("Gov scraper — skipping image content-type at: %s", url)
        resp.close()
        return ''

    return _extract_html_text(resp.text, max_chars)


# ── Page scrapers ─────────────────────────────────────────────────────────────

def _scrape_rss(agency, max_age_days=45):
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    cutoff = _dt.now(_tz.utc) - _td(days=max_age_days)
    try:
        import feedparser
        feed = feedparser.parse(agency.rss_url)
        items = []
        for entry in feed.entries[:15]:
            title = entry.get('title', '').strip()
            url = entry.get('link', '').strip()
            if not title or not url:
                continue

            pub_iso = None
            published = entry.get('published_parsed') or entry.get('updated_parsed')
            if published:
                try:
                    pub_dt = _dt(*published[:6], tzinfo=_tz.utc)
                    if pub_dt < cutoff:
                        logger.debug("Gov RSS — skipping stale entry (%s): %s", agency.acronym, title[:60])
                        continue
                    pub_iso = pub_dt.isoformat()
                except Exception:
                    pass

            summary = entry.get('summary', '') or entry.get('description', '')
            items.append({'title': title, 'url': url, 'summary': summary[:800], 'published_iso': pub_iso})
        return items
    except Exception as exc:
        logger.error("RSS scrape failed for %s: %s", agency.acronym, exc)
        return []


def _scrape_html(agency):
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.error("beautifulsoup4 not installed")
        return []

    try:
        resp = requests.get(agency.news_url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', '').lower()
        if 'application/pdf' in content_type or 'image/' in content_type:
            logger.warning("%s news_url returned non-HTML content — skipping", agency.acronym)
            return []
    except Exception as exc:
        logger.warning("HTTP fetch failed for %s (%s): %s", agency.acronym, agency.news_url, exc)
        return []

    try:
        soup = BeautifulSoup(resp.text, 'lxml')
    except Exception:
        soup = BeautifulSoup(resp.text, 'html.parser')

    base = agency.website.rstrip('/')
    items = []

    # Strategy 1: <article> tags
    articles = soup.find_all('article', limit=10)
    if articles:
        for art in articles:
            heading = art.find(['h1', 'h2', 'h3', 'h4'])
            link_tag = (heading.find('a', href=True) if heading else None) or art.find('a', href=True)
            if not link_tag:
                continue
            title = (heading or link_tag).get_text(strip=True)
            if len(title) < 10:
                continue
            href = link_tag.get('href', '')
            if not href or href.startswith('#') or href.startswith('javascript'):
                continue
            p_tag = art.find('p')
            items.append({
                'title': title,
                'url': _absolute(href, base),
                'summary': p_tag.get_text(strip=True)[:500] if p_tag else '',
            })
        if items:
            return items

    # Strategy 2: divs/lis/sections with news-like class names
    news_keywords = ['news', 'press', 'post', 'article', 'item', 'entry', 'release', 'story', 'update']
    for kw in news_keywords:
        containers = soup.find_all(
            ['div', 'li', 'section'],
            class_=lambda c, k=kw: c and k in c.lower(),
            limit=12,
        )
        if not containers:
            continue
        for container in containers:
            heading = container.find(['h1', 'h2', 'h3', 'h4'])
            link_tag = (heading.find('a', href=True) if heading else None) or container.find('a', href=True)
            if not link_tag:
                continue
            href = link_tag.get('href', '')
            if not href or href.startswith('#') or href.startswith('javascript'):
                continue
            title = (heading or link_tag).get_text(strip=True)
            if len(title) < 10:
                continue
            p_tag = container.find('p')
            items.append({
                'title': title,
                'url': _absolute(href, base),
                'summary': p_tag.get_text(strip=True)[:500] if p_tag else '',
            })
        if items:
            return items

    # Strategy 3: headings containing links (last-resort fallback)
    for heading in soup.find_all(['h2', 'h3', 'h4'], limit=20):
        link_tag = heading.find('a', href=True)
        if not link_tag:
            parent = heading.parent
            link_tag = parent.find('a', href=True) if parent else None
        if not link_tag:
            continue
        href = link_tag.get('href', '')
        if not href or href.startswith('#') or href.startswith('javascript'):
            continue
        title = heading.get_text(strip=True)
        if len(title) < 10:
            continue
        items.append({
            'title': title,
            'url': _absolute(href, base),
            'summary': '',
        })

    return items[:10]


def scrape_agency(agency):
    """Return list of {title, url, summary} dicts from an agency's news page."""
    if agency.scrape_strategy == 'rss' and agency.rss_url:
        return _scrape_rss(agency)
    return _scrape_html(agency)
