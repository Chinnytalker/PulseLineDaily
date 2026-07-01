"""
Data journalism helpers: fetch open data + RSS stories, build Claude prompts.
No Django models imported here — keep this as pure Python / easily testable.
"""

import html as html_module
import logging
import re
import requests
from html.parser import HTMLParser

logger = logging.getLogger(__name__)


# ── Shared LLM response parser ────────────────────────────────────────────────

def parse_llm_response(raw):
    """
    Parse LLM output into (summary, analysis, html_body).

    Expected format:
        SUMMARY: <text>
        ANALYSIS: <text>
        ---
        <HTML body starting with <h2>>

    If the LLM omits the '---' separator (common), falls back to regex
    extraction so SUMMARY/ANALYSIS never bleed into the body field.
    """
    raw = (raw or "").strip()
    summary, analysis, html = "", "", raw

    if "---" in raw:
        head, body = raw.split("---", 1)
        html = body.strip()
        current_key, buf = None, []
        for line in head.splitlines():
            stripped = line.strip()
            upper = stripped.upper()
            if upper.startswith("SUMMARY:"):
                current_key = "summary"
                buf = [stripped[8:].strip()]
            elif upper.startswith("ANALYSIS:"):
                if current_key == "summary":
                    summary = " ".join(buf).strip()
                current_key = "analysis"
                buf = [stripped[9:].strip()]
            elif current_key and stripped:
                buf.append(stripped)
        if current_key == "summary":
            summary = " ".join(buf).strip()
        elif current_key == "analysis":
            analysis = " ".join(buf).strip()
    else:
        # Fallback: extract SUMMARY/ANALYSIS via regex, body = everything from first HTML tag
        s_match = re.search(
            r'(?im)^SUMMARY\s*:\s*(.+?)(?=\n\s*ANALYSIS\s*:|\Z)', raw, re.DOTALL
        )
        a_match = re.search(
            r'(?im)^ANALYSIS\s*:\s*(.+?)(?=\n\s*<[a-z]|\Z)', raw, re.DOTALL
        )
        if s_match:
            summary = s_match.group(1).strip()
        if a_match:
            analysis = a_match.group(1).strip()
        body_match = re.search(r'<(?:h[1-6]|p|div|ul|section)\b', raw, re.IGNORECASE)
        if body_match:
            html = raw[body_match.start():].strip()

    return summary, analysis, html


# ── RSS Sources (Option C) ────────────────────────────────────────────────────
# Add / remove feeds freely.  category → matched against your Category table.
RSS_SOURCES = [
    # ── News ─────────────────────────────────────────────────────────────────
    {"url": "https://punchng.com/feed/",                        "category": "News",          "label": "Punch NG"},
    {"url": "https://www.vanguardngr.com/feed/",                "category": "News",          "label": "Vanguard NG"},
    {"url": "https://www.channelstv.com/feed/",                 "category": "News",          "label": "Channels TV"},
    {"url": "https://guardian.ng/feed/",                        "category": "News",          "label": "Guardian NG"},
    {"url": "https://www.premiumtimesng.com/feed/",             "category": "News",          "label": "Premium Times NG"},
    # ── Politics ─────────────────────────────────────────────────────────────
    {"url": "https://dailypost.ng/feed/",                       "category": "Politics",      "label": "Daily Post NG"},
    {"url": "https://www.thecable.ng/feed/",                    "category": "Politics",      "label": "The Cable NG"},
    # ── Economy ──────────────────────────────────────────────────────────────
    {"url": "https://businessday.ng/feed/",                     "category": "Economy",       "label": "BusinessDay NG"},
    # ── Technology ───────────────────────────────────────────────────────────
    {"url": "https://techcabal.com/feed/",                      "category": "Technology",    "label": "TechCabal"},
    # ── Entertainment ────────────────────────────────────────────────────────
    {"url": "https://www.pulse.ng/entertainment/rss",           "category": "Entertainment", "label": "Pulse NG Entertainment"},
    {"url": "https://www.bellanaija.com/feed/",                 "category": "Entertainment", "label": "BellaNaija"},
    {"url": "https://notjustok.com/feed/",                      "category": "Entertainment", "label": "NotJustOK"},
    {"url": "https://guardian.ng/art/feed/",                    "category": "Entertainment", "label": "Guardian Arts & Entertainment"},
    # ── Education ────────────────────────────────────────────────────────────
    {"url": "https://www.legit.ng/rss/all.rss",                 "category": "Education",     "label": "Legit NG"},
    {"url": "https://saharareporters.com/feed",                 "category": "News",          "label": "Sahara Reporters"},
    {"url": "https://www.thecable.ng/category/education/feed/", "category": "Education",     "label": "The Cable Education"},
]

WORLDBANK_BASE = "https://api.worldbank.org/v2"

# Curated World Bank indicators for Nigeria data journalism.
# Each entry rotates on a different day of the year.
INDICATORS = {
    # ── Economy ──────────────────────────────────────────────────────
    "inflation": {
        "code": "FP.CPI.TOTL.ZG",
        "name": "Inflation Rate",
        "unit": "%",
        "category": "Economy",
        "context": (
            "Consumer price inflation measures the annual percentage rise in prices. "
            "High inflation erodes purchasing power and hits low-income households hardest."
        ),
    },
    "gdp_growth": {
        "code": "NY.GDP.MKTP.KD.ZG",
        "name": "GDP Growth Rate",
        "unit": "%",
        "category": "Economy",
        "context": (
            "GDP growth rate shows how fast Nigeria's total economic output is expanding "
            "or contracting each year."
        ),
    },
    "unemployment": {
        "code": "SL.UEM.TOTL.ZS",
        "name": "Unemployment Rate",
        "unit": "%",
        "category": "Economy",
        "context": (
            "The unemployment rate is the share of the labour force that is jobless "
            "and actively seeking work."
        ),
    },
    "gdp_per_capita": {
        "code": "NY.GDP.PCAP.CD",
        "name": "GDP Per Capita",
        "unit": " USD",
        "category": "Economy",
        "context": (
            "GDP per capita divides the total economy by population, giving an approximate "
            "measure of average living standards."
        ),
    },
    "poverty": {
        "code": "SI.POV.DDAY",
        "name": "Poverty Headcount Ratio",
        "unit": "% of population",
        "category": "Economy",
        "context": (
            "The share of Nigerians living on less than $2.15 a day "
            "(World Bank international poverty line)."
        ),
    },
    "external_debt": {
        "code": "DT.DOD.DECT.CD",
        "name": "External Debt Stock",
        "unit": " USD (current)",
        "category": "Economy",
        "context": (
            "Nigeria's total external debt owed to foreign creditors, "
            "which affects fiscal policy, exchange rates, and development spending."
        ),
    },
    # ── Health ───────────────────────────────────────────────────────
    "life_expectancy": {
        "code": "SP.DYN.LE00.IN",
        "name": "Life Expectancy at Birth",
        "unit": " years",
        "category": "Health",
        "context": (
            "Life expectancy at birth is one of the most important indicators "
            "of a nation's overall health and healthcare quality."
        ),
    },
    "infant_mortality": {
        "code": "SP.DYN.IMRT.IN",
        "name": "Infant Mortality Rate",
        "unit": " per 1,000 live births",
        "category": "Health",
        "context": (
            "Infant mortality rate measures deaths of children under one year old "
            "per 1,000 live births — a key indicator of maternal and child health."
        ),
    },
    "health_expenditure": {
        "code": "SH.XPD.CHEX.GD.ZS",
        "name": "Health Expenditure",
        "unit": "% of GDP",
        "category": "Health",
        "context": (
            "Current health expenditure as a percentage of GDP, "
            "combining both government and private health spending."
        ),
    },
    # ── Education ────────────────────────────────────────────────────
    "literacy_rate": {
        "code": "SE.ADT.LITR.ZS",
        "name": "Adult Literacy Rate",
        "unit": "%",
        "category": "Education",
        "context": (
            "The percentage of people aged 15 and above who can read and write — "
            "a fundamental measure of human capital development."
        ),
    },
    "primary_enrollment": {
        "code": "SE.PRM.ENRR",
        "name": "Primary School Enrollment",
        "unit": "%",
        "category": "Education",
        "context": (
            "Gross enrollment ratio in primary education, showing access to basic schooling."
        ),
    },
    # ── Infrastructure & Digital ─────────────────────────────────────
    "electricity_access": {
        "code": "EG.ELC.ACCS.ZS",
        "name": "Access to Electricity",
        "unit": "% of population",
        "category": "Infrastructure",
        "context": (
            "The percentage of Nigeria's population with access to electricity — "
            "critical for economic productivity, healthcare, and quality of life."
        ),
    },
    "internet_users": {
        "code": "IT.NET.USER.ZS",
        "name": "Internet Users",
        "unit": "% of population",
        "category": "Technology",
        "context": (
            "The share of Nigerians using the internet, "
            "reflecting digital inclusion and the country's technology adoption."
        ),
    },
    "mobile_subscriptions": {
        "code": "IT.CEL.SETS.P2",
        "name": "Mobile Phone Subscriptions",
        "unit": " per 100 people",
        "category": "Technology",
        "context": (
            "Mobile cellular subscriptions per 100 people — "
            "a measure of telecommunications reach and digital connectivity."
        ),
    },
    # ── Government Finance ───────────────────────────────────────────
    "government_revenue": {
        "code": "GC.REV.TOTL.GD.ZS",
        "name": "Government Revenue",
        "unit": "% of GDP",
        "category": "Economy",
        "context": (
            "Government revenue as a percentage of GDP shows how much of Nigeria's "
            "total economic output the government collects — key to funding public "
            "services, infrastructure, and debt repayment."
        ),
    },
    "government_expenditure": {
        "code": "GC.XPN.TOTL.GD.ZS",
        "name": "Government Expenditure",
        "unit": "% of GDP",
        "category": "Economy",
        "context": (
            "Government expenditure as a share of GDP reflects the size of Nigeria's "
            "public sector and its fiscal policy stance — higher spending can stimulate "
            "growth but risks widening the deficit."
        ),
    },
    "government_debt": {
        "code": "GC.DOD.TOTL.GD.ZS",
        "name": "Government Debt",
        "unit": "% of GDP",
        "category": "Economy",
        "context": (
            "Nigeria's central government debt as a share of GDP — a key measure of "
            "fiscal sustainability and the country's ability to service obligations "
            "without crowding out development spending."
        ),
    },
    "lending_rate": {
        "code": "FR.INR.LEND",
        "name": "Bank Lending Interest Rate",
        "unit": "%",
        "category": "Economy",
        "context": (
            "The average bank lending rate reflects the cost of borrowing in Nigeria, "
            "shaped by CBN monetary policy. High rates constrain business investment "
            "and consumer credit, while low rates can fuel inflation."
        ),
    },
    "oil_rents": {
        "code": "NY.GDP.PETR.RT.ZS",
        "name": "Oil Rents",
        "unit": "% of GDP",
        "category": "Economy",
        "context": (
            "Oil rents as a share of GDP show how dependent Nigeria's economy is "
            "on petroleum revenues — a structural challenge the government has long "
            "sought to diversify away from."
        ),
    },
}


def fetch_worldbank_indicator(indicator_code, country="NGA", mrv=6):
    """
    Return a list of {year, value} dicts sorted oldest → newest,
    or None if the request fails or returns no data.
    """
    url = f"{WORLDBANK_BASE}/country/{country}/indicator/{indicator_code}"
    params = {"format": "json", "mrv": mrv, "per_page": mrv}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        if len(payload) < 2 or not payload[1]:
            return None
        points = [
            {"year": int(item["date"]), "value": round(float(item["value"]), 2)}
            for item in payload[1]
            if item["value"] is not None
        ]
        return sorted(points, key=lambda p: p["year"]) or None
    except Exception as exc:
        logger.warning("World Bank [%s] error: %s", indicator_code, exc)
        return None


def fetch_usd_ngn_rate():
    """
    Return {rate, updated} for USD → NGN, or None on failure.
    Uses open.er-api.com (no API key required).
    """
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=12)
        resp.raise_for_status()
        data = resp.json()
        ngn = data["rates"].get("NGN")
        if not ngn:
            return None
        return {
            "rate": round(float(ngn), 2),
            "updated": data.get("time_last_update_utc", ""),
        }
    except Exception as exc:
        logger.warning("Exchange rate API error: %s", exc)
        return None


# ── Prompt builders ───────────────────────────────────────────────────────────

def build_indicator_prompt(indicator_key, info, data_points):
    latest = data_points[-1]
    prev = data_points[-2] if len(data_points) >= 2 else None

    history_lines = "\n".join(
        f"  {p['year']}: {p['value']}{info['unit']}" for p in data_points
    )
    trend_sentence = ""
    if prev and prev["value"]:
        delta = latest["value"] - prev["value"]
        direction = "rose" if delta > 0 else "fell"
        trend_sentence = (
            f"The figure {direction} by {abs(delta):.2f}{info['unit']} "
            f"compared to {prev['year']}."
        )

    return f"""You are a professional data journalist writing for PulseLineDaily, a leading Nigerian digital news outlet.

Write a complete, publish-ready HTML news article about Nigeria's {info['name']} using official World Bank data.

IMPORTANT DATA CONTEXT: World Bank data is released 1–3 years after the reference year due to collection and verification delays. The most recent available figure is from {latest['year']}. This is normal and does NOT mean the data is outdated — it is the official, verified record. Frame the article as an analysis of the latest available World Bank data, NOT as breaking news. Always state the year ({latest['year']}) clearly so readers know the reference period.

INDICATOR: {info['name']}
BACKGROUND: {info['context']}

WORLD BANK DATA — Nigeria (NGA):
{history_lines}

Latest available: {latest['value']}{info['unit']} ({latest['year']}). {trend_sentence}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <one punchy sentence max 160 characters — must include the year {latest['year']} so readers know the data period; no "PulseLineDaily" branding>
ANALYSIS: <2–3 sentences of sharp expert takeaway — what this figure reveals about Nigeria's trajectory and what to watch going forward; no named quotes>
---
<full HTML article body starting with <h2>>

ARTICLE REQUIREMENTS:
- Pure HTML body content — use <h2>, <h3>, <p>, <strong>, <ul>, <li>
- Do NOT include <html>, <body>, <head>, or <style> tags
- Open with a strong <h2> headline that names the key figure AND the year (e.g. "Nigeria's X in {latest['year']}: What the Data Shows")
- Paragraph 1: state the statistic clearly, name the year {latest['year']}, explain this is the latest available World Bank figure, and why the trend it reveals still matters today
- <h3>Trend Analysis</h3>: compare the full data series; note whether the trajectory is improving, worsening, or stable — and what that direction means going forward
- <h3>What This Means for Everyday Nigerians</h3>: 4–5 concrete implications for consumers, workers, businesses, or families — use bullet points
- <h3>Expert Perspective</h3>: 3–4 sentences of authoritative commentary — do NOT invent named quotes or named individuals
- <h3>What to Watch Next</h3>: what indicators or policy changes Nigerians should track to see if the trend is continuing or reversing
- Attribute data to the World Bank and note the {latest['year']} reference year
- Length: 600–800 words
- Tone: analytical, authoritative, accessible to a general Nigerian audience — trend analysis, not breaking news
- Naturally include SEO keywords: "Nigeria {info['name'].lower()} {latest['year']}", "{info['category'].lower()} Nigeria data"
- Do NOT invent statistics beyond the data provided above
- Do NOT write phrases like "latest data shows" or "recent figures reveal" without specifying the year {latest['year']}"""


def build_exchange_rate_prompt(rate_data):
    rate = rate_data["rate"]
    return f"""You are a professional financial journalist writing for PulseLineDaily, a leading Nigerian digital news outlet.

Write a complete, publish-ready HTML news article about today's USD to Nigerian Naira exchange rate.

DATA (Open Exchange Rates — live):
- 1 USD = ₦{rate:,.2f}
- Updated: {rate_data['updated']}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <one punchy sentence max 160 characters — the article card teaser, include the exact rate>
ANALYSIS: <2–3 sentences of sharp expert takeaway — what this rate signals about the Nigerian economy and what consumers and businesses should do>
---
<full HTML article body starting with <h2>>

ARTICLE REQUIREMENTS:
- Pure HTML body content — use <h2>, <h3>, <p>, <strong>, <ul>, <li>
- Do NOT include <html>, <body>, <head>, or <style> tags
- Open with a strong <h2> headline that includes the exact rate
- Paragraph 1: state the rate and its immediate significance for Nigeria
- <h3>Impact on Imports and Everyday Goods</h3>: explain how this rate affects food prices, fuel, electronics, and household goods — use bullet points
- <h3>What Should Nigerians Do?</h3>: 4–5 practical financial tips for individuals, traders, importers, and small businesses
- <h3>What the CBN and Government Are Doing</h3>: general context on monetary policy and exchange rate management — do NOT fabricate specific policy announcements
- <h3>Outlook</h3>: a cautious, general statement about currency dynamics — do NOT predict a specific rate or fabricate expert quotes
- Attribute data to Open Exchange Rates
- Length: 500–650 words
- Tone: helpful, factual, practical
- SEO keywords: "naira exchange rate today", "dollar to naira", "USD NGN", "CBN exchange rate" """


# ── Naira parallel (black) market rate ───────────────────────────────────────

def fetch_parallel_market_rate():
    """
    Fetch USD/NGN parallel market rate from AbokiFX and compare with the official rate.
    Returns dict with official, parallel, and premium_pct fields, or None on total failure.
    """
    official_data = fetch_usd_ngn_rate()
    official = official_data["rate"] if official_data else None

    parallel = None
    # Years that commonly appear on financial pages and must not be treated as rates
    _YEAR_BLACKLIST = set(range(2015, 2031))

    try:
        resp = requests.get(
            "https://abokifx.com/",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=12,
        )
        resp.raise_for_status()

        # Separate candidates with decimals (real rates) from bare integers (may be years)
        decimal_pool = []
        integer_pool = []
        for int_part, dec_part in re.findall(r'\b([12]\d{3})([.,]\d{1,2})?\b', resp.text):
            int_val = int(int_part)
            if not (1000 <= int_val <= 2999):
                continue
            # Bare integer that matches a calendar year — skip
            if not dec_part and int_val in _YEAR_BLACKLIST:
                continue
            val = float(int_part + (dec_part.replace(",", ".") if dec_part else ""))
            (decimal_pool if dec_part else integer_pool).append(val)

        # Prefer decimal candidates; fall back to integers only if necessary
        pool = decimal_pool if decimal_pool else integer_pool
        if pool:
            pool.sort()
            candidate = round(pool[len(pool) // 2], 2)
            # Sanity-check: parallel rate must be 2–80% above the official rate
            if official:
                premium = (candidate - official) / official
                if 0.02 <= premium <= 0.80:
                    parallel = candidate
                else:
                    logger.warning(
                        "AbokiFX candidate ₦%s rejected — premium %.1f%% outside 2–80%% band vs official ₦%s",
                        candidate, premium * 100, official,
                    )
            else:
                parallel = candidate
    except Exception as exc:
        logger.warning("AbokiFX parallel rate fetch failed: %s", exc)

    if not official and not parallel:
        return None

    premium_pct = None
    if official and parallel:
        premium_pct = round(((parallel - official) / official) * 100, 1)

    return {
        "official": official,
        "parallel": parallel,
        "premium_pct": premium_pct,
        "updated": (official_data or {}).get("updated", ""),
    }


def build_parallel_rate_prompt(data, date_str):
    official = data.get("official")
    parallel = data.get("parallel")
    premium = data.get("premium_pct")

    official_line = f"₦{official:,.2f}" if official else "unavailable"
    parallel_line = f"₦{parallel:,.2f}" if parallel else "unavailable (use general market context)"
    premium_line = (
        f"{premium:+.1f}% above the official rate"
        if premium is not None else "premium spread unavailable"
    )

    return f"""You are a senior financial journalist at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, publish-ready HTML article comparing today's official CBN exchange rate with the Nigerian parallel (black) market rate.

TODAY: {date_str}

LIVE RATE DATA (use these exact figures — do NOT invent or adjust them):
- Official CBN / interbank rate: 1 USD = {official_line}
- Parallel (black) market rate:  1 USD = {parallel_line}
- Parallel market premium: {premium_line}
- Data sources: Open Exchange Rates (official), AbokiFX (parallel market)

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <one punchy sentence max 160 characters — include both rates or the premium spread>
ANALYSIS: <2–3 sentences of expert takeaway — what the gap between rates reveals about Nigeria's forex situation>
---
<full HTML article body starting with <h2>>

ARTICLE REQUIREMENTS:
- Pure HTML — every section title MUST be in <h3>...</h3> tags, every paragraph in <p>...</p>, every list in <ul><li>...</li></ul>
- Do NOT include <html>, <body>, <head>, or <style> tags
- Do NOT use plain bold text as section headers — use <h3> tags only
- <h2>: headline naming both rates — e.g. "Black Market Dollar Rate Today: ₦X,XXX as CBN Rate Sits at ₦Y,YYY"
- <p> Opening: state both rates from the data above and the exact premium spread — use the figures provided, not invented ones
- <h3>Why Two Rates Exist</h3>: <p> explaining the dual exchange rate system in plain language for everyday Nigerians
- <h3>What This Means for You</h3>: <ul> with one <li> per group — importers, travellers, students abroad, online shoppers, remittance receivers — specific impact for each
- <h3>What Is Driving the Gap</h3>: <p> on structural causes — forex scarcity, CBN policy, oil revenue, demand pressure — established economic context only; do NOT fabricate specific CBN announcements
- <h3>CBN's Position and What to Watch</h3>: <ul> with 3–4 <li> indicators Nigerians should monitor for rate stability or further pressure
- Closing <p>: a sharp, practical sentence on what Nigerians should do with this information today — do NOT open with "In conclusion"
- Attribute official rate to Open Exchange Rates; parallel rate to AbokiFX
- Length: 700–900 words | Tone: practical, informative, authoritative
- SEO: "dollar to naira black market today", "parallel market rate", "aboki dollar rate", "cbn exchange rate", "naira black market"
- Do NOT fabricate specific CBN policy announcements, named quotes, or rate predictions"""


# ── Option C: RSS fetch + AI rewrite ─────────────────────────────────────────

def _strip_html(raw):
    """Remove HTML tags and decode entities from an RSS excerpt."""
    text = html_module.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class _ArticleTextExtractor(HTMLParser):
    """Strip nav/footer/script noise and collect readable article text."""
    _SKIP = {"script", "style", "nav", "header", "footer", "aside", "noscript", "iframe"}

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


def fetch_article_body(url, max_chars=3000):
    """
    Fetch the readable text of a news article URL.
    Used to give the LLM specific facts (names, figures, rankings) from the source.
    Returns up to max_chars of cleaned text, or empty string on failure.
    """
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=12)
        resp.raise_for_status()
        extractor = _ArticleTextExtractor()
        extractor.feed(html_module.unescape(resp.text))
        text = " ".join(extractor.parts)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as exc:
        logger.debug("Article body fetch failed [%s]: %s", url, exc)
        return ""


def fetch_rss_stories(max_per_source=2):
    """
    Fetch recent stories from RSS_SOURCES using feedparser.
    Returns a list of dicts: {title, excerpt, link, source_label, category}.
    Silently skips any feed that fails or if feedparser is not installed.
    """
    try:
        import feedparser
    except ImportError:
        logger.error("feedparser not installed — run: pip install feedparser")
        return []

    stories = []
    for source in RSS_SOURCES:
        try:
            feed = feedparser.parse(source["url"])
            entries = feed.entries[:max_per_source]
            for entry in entries:
                title = (entry.get("title") or "").strip()
                if not title:
                    continue
                # Prefer full content, then summary, then description
                raw_excerpt = (
                    (entry.get("content") or [{}])[0].get("value", "")
                    or entry.get("summary")
                    or entry.get("description")
                    or ""
                )
                excerpt = _strip_html(raw_excerpt)[:1500]
                link = entry.get("link") or ""
                if not link:
                    continue
                # body_text is fetched lazily in the task (after dedup checks pass)
                stories.append({
                    "title": title,
                    "excerpt": excerpt,
                    "link": link,
                    "source_label": source["label"],
                    "category": source["category"],
                })
        except Exception as exc:
            logger.warning("RSS fetch error [%s]: %s", source["label"], exc)

    return stories


def build_rewrite_prompt(story):
    """
    Build a prompt that produces a genuinely original, AdSense-quality PulseLineDaily article.
    Uses the full article body so the LLM can cite specific facts rather than writing generically.
    """
    full_body = (story.get("body_text") or "").strip()
    if full_body:
        source_block = (
            f"SOURCE ARTICLE CONTENT from {story['source_label']} "
            f"(use facts and names — every sentence you write must be your own words):\n{full_body}"
        )
    else:
        source_block = (
            f"RSS EXCERPT from {story['source_label']} "
            f"(limited context — use what is available):\n{story['excerpt']}"
        )

    return f"""You are a senior journalist at PulseLineDaily, a leading Nigerian digital news outlet. Your articles are published, indexed by Google, and reviewed for AdSense quality — they must be genuinely original, substantive, and add clear value beyond what the source article says.

STORY TOPIC:
Headline: {story['title']}
Source: {story['source_label']}

{source_block}

YOUR TASK:
Write a 900–1100 word fully original HTML news article. Your article must:
1. Use every specific fact from the source (names, figures, rankings, dates, institutions) — never skip specifics
2. Add original editorial context, historical background, or expert-level analysis NOT present in the source
3. Offer a perspective or angle that makes this article more valuable than the source itself
4. Read like it was written by an experienced Nigerian journalist, not a summary bot

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <one punchy sentence max 160 characters — the story hook, specific not vague>
ANALYSIS: <2–3 sentences of original editorial analysis — insight a reader won't find in the source article>
---
<full HTML article body starting with <h2>>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li> — no <html>/<body>/<head>/<style> tags
- <h2>: original headline — rewrite the source headline with a fresh angle
- Paragraph 1: gripping lead naming the single most important specific fact (person, figure, institution, or event) from the source
- <h3>Key Facts</h3>: bullet list of ALL specific entities, names, figures, statistics, rankings, or dates from the source — never be vague when specifics exist; if the story names 24 universities, list them; if it names officials, name them
- <h3>Background and Context</h3>: 2–3 paragraphs of original context — historical precedent, how this fits a bigger trend, what led to this development; this section must go BEYOND the source article
- <h3>Why This Matters to Nigerians</h3>: 4–5 concrete, specific implications — economic impact, social consequence, political significance; avoid generic statements like "this is important" — say WHY and HOW
- <h3>Expert Perspective</h3>: 3–4 sentences of original authoritative analysis — what this development reveals, what risks or opportunities it creates; do NOT invent named quotes or attribute views to specific people
- <h3>What Happens Next</h3>: specific, informed analysis of the next steps, timeline, or likely developments — not vague speculation
- Closing paragraph: a sharp, memorable closing sentence that reinforces the article's core message
- Tone: authoritative, intelligent, engaging — the voice of Nigeria's best journalism
- Do NOT copy sentences from the source, do NOT be vague when facts are available, do NOT pad with filler
- Include natural SEO keywords relevant to the Nigerian context throughout the article"""


# ── Sports helpers ────────────────────────────────────────────────────────────

# Keywords that reliably identify a sports story in the title or excerpt.
SPORTS_KEYWORDS = frozenset([
    "football", "soccer", "basketball", "tennis", "cricket", "rugby", "golf",
    "athletics", "swimming", "boxing", "wrestling", "cycling", "volleyball",
    # competitions
    "world cup", "premier league", "champions league", "europa league", "laliga",
    "bundesliga", "serie a", "ligue 1", "afcon", "caf", "fifa", "uefa", "copa",
    "olympic", "commonwealth games", "super bowl", "nba", "nfl", "wimbledon",
    # nigerian / african sport
    "super eagles", "flying eagles", "super falcons", "super six", "npfl",
    "aiteo cup", "caf champions", "wafu", "chan",
    # generic match terms — specific enough to avoid false positives
    "match report", "hat-trick", "hat trick", "penalty shootout",
    "scored a goal", "scored goals", "own goal",
    "transfer fee", "transfer window", "footballer", "striker",
    "midfielder", "defender", "goalkeeper", "manager sacked", "coach sacked",
    # popular clubs covered by Nigerian press
    "arsenal", "chelsea", "manchester united", "manchester city", "liverpool",
    "tottenham", "barcelona", "real madrid", "psg", "juventus", "ac milan",
    "inter milan", "bayern munich", "borussia dortmund",
])

POLITICS_KEYWORDS = frozenset([
    # Nigerian governance
    "president tinubu", "bola tinubu", "aso rock", "state house",
    "national assembly", "house of representatives", "senate", "senator",
    "governor", "deputy governor", "minister", "ministerial", "cabinet",
    "inec", "electoral commission", "election", "voting", "ballot", "polling",
    "by-election", "governorship election", "presidential election",
    "apc", "pdp", "labour party", "nnpp", "apga", "accord party",
    "political party", "opposition", "ruling party",
    # Nigerian political figures / roles
    "attorney general", "chief of staff", "state governor",
    "house speaker", "senate president", "deputy senate president",
    "fcta", "abuja", "statehouse", "presidency",
    # political events / processes
    "impeachment", "motion of no confidence", "budget passage",
    "constitutional amendment", "legislation", "bill passed", "bill signed",
    "executive order", "policy announcement", "government policy",
    "corruption", "anti-corruption", "efcc", "icpc", "dss", "nsa",
    "protest", "demonstration", "strike", "labour union", "nlc", "tuc",
    "coup", "insurrection", "amnesty", "pardon",
    # subnational politics
    "local government", "lga", "state house of assembly",
])

TECHNOLOGY_KEYWORDS = frozenset([
    "startup", "fintech", "techcabal", "paystack", "flutterwave", "opay",
    "kuda", "piggyvest", "moniepoint", "palmpay",
    "artificial intelligence", "machine learning", "blockchain", "crypto",
    "bitcoin", "ethereum", "web3", "nft",
    "5g", "broadband", "fibre", "internet access", "data plan",
    "app launch", "app update", "software", "saas", "cloud",
    "e-commerce", "jumia", "konga", "jiji",
    "ride-hailing", "bolt", "uber", "gokada",
    "edtech", "healthtech", "agritech", "insurtech",
    "mtn", "airtel", "glo", "9mobile", "telecom",
    "ncc", "nitda", "digital economy",
])


EDUCATION_KEYWORDS = frozenset([
    "jamb", "waec", "neco", "nysc", "utme", "post-utme", "admission",
    "university", "polytechnic", "college of education", "student loan",
    "scholarship", "bursary", "school fees", "examination", "exam result",
    "results released", "matriculation", "convocation", "graduation",
    "ministry of education", "tetfund", "jamb result", "waec result",
    "neco result", "nysc orientation", "nysc mobilisation", "ppa",
    "federal university", "state university", "private university",
    "accreditation", "jupeb", "ijmb", "a-level", "o-level",
    "primary school", "secondary school", "cut-off mark",
])

ENTERTAINMENT_KEYWORDS = frozenset([
    # music / afrobeats
    "music", "album", "single", "song", "afrobeats", "afropop", "burna boy",
    "wizkid", "davido", "asake", "olamide", "tiwa savage", "ckay", "rema",
    "tems", "ayra starr", "grammy", "billboard", "spotify", "apple music",
    "concert", "tour", "lyrics", "music video", "bts", "streaming",
    # nollywood / film
    "nollywood", "movie", "film", "cinema", "box office", "actor", "actress",
    "director", "netflix", "amazon prime", "showmax", "series", "episode",
    "award", "amvca", "afrima", "vgma",
    # celebrity / lifestyle
    "celebrity", "wedding", "engagement", "divorce", "fashion", "style",
    "beauty", "makeup", "red carpet", "interview", "bbnaija", "big brother",
    "reality show", "influencer", "skit maker", "comedian", "stand-up",
    # arts / culture
    "art exhibition", "gallery", "theatre", "book", "author", "novel",
    "literary", "cultural festival", "heritage",
])


def detect_story_category(title, excerpt, default_category):
    """
    Check title + excerpt against keyword sets in priority order.
    Sports > Politics > Technology > Entertainment > Education; falls back to RSS source default.
    """
    text = (title + " " + excerpt).lower()
    if any(kw in text for kw in SPORTS_KEYWORDS):
        return "Sports"
    if any(kw in text for kw in POLITICS_KEYWORDS):
        return "Politics"
    if any(kw in text for kw in TECHNOLOGY_KEYWORDS):
        return "Technology"
    if any(kw in text for kw in ENTERTAINMENT_KEYWORDS):
        return "Entertainment"
    if any(kw in text for kw in EDUCATION_KEYWORDS):
        return "Education"
    return default_category


# ── Live sports data fetchers ─────────────────────────────────────────────────

def fetch_epl_standings():
    """
    Fetch Premier League standings from ESPN's public API (no key needed).
    Returns a list of dicts sorted by position, or None on failure.
    """
    url = "https://site.api.espn.com/apis/v2/sports/soccer/eng.1/standings"
    try:
        resp = requests.get(url, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        entries = []
        # ESPN returns standings under "children[].standings.entries" for
        # multi-group sports, but EPL uses "standings.entries" directly.
        groups = data.get("children") or [data]
        for group in groups:
            for entry in group.get("standings", {}).get("entries", []):
                name = entry.get("team", {}).get("displayName", "Unknown")
                stats = {s["name"]: s["value"] for s in entry.get("stats", [])}
                entries.append({
                    "team": name,
                    "played": int(stats.get("gamesPlayed", 0)),
                    "points": int(stats.get("points", 0)),
                    "wins": int(stats.get("wins", 0)),
                    "draws": int(stats.get("ties", 0)),
                    "losses": int(stats.get("losses", 0)),
                    "gf": int(stats.get("pointsFor", 0)),
                    "ga": int(stats.get("pointsAgainst", 0)),
                })
        return sorted(entries, key=lambda x: x["points"], reverse=True)[:20] or None
    except Exception as exc:
        logger.warning("ESPN EPL standings error: %s", exc)
        return None


def fetch_nigeria_results():
    """
    Fetch Nigeria Super Eagles recent results from TheSportsDB (free, no key).
    Returns {team, results: [...]} or None on failure.
    """
    try:
        # Find Nigeria football team ID
        search = requests.get(
            "https://www.thesportsdb.com/api/v1/json/3/searchteams.php",
            params={"t": "Nigeria"},
            timeout=12,
        )
        search.raise_for_status()
        teams = search.json().get("teams") or []
        team = next(
            (t for t in teams
             if "soccer" in (t.get("strSport") or "").lower()
             or "football" in (t.get("strSport") or "").lower()),
            None,
        )
        if not team:
            return None

        team_id = team["idTeam"]
        evts = requests.get(
            "https://www.thesportsdb.com/api/v1/json/3/eventslast.php",
            params={"id": team_id},
            timeout=12,
        )
        evts.raise_for_status()
        raw = evts.json().get("results") or []
        if not raw:
            return None

        # Freshness gate: skip if the most recent match is older than 60 days
        from datetime import datetime as _dt, timedelta as _td
        most_recent_date = raw[-1].get("dateEvent", "")
        if most_recent_date:
            try:
                age_days = (_dt.utcnow() - _dt.strptime(most_recent_date, "%Y-%m-%d")).days
                if age_days > 60:
                    logger.info(
                        "Super Eagles results stale (%s, %d days old) — skipping article",
                        most_recent_date, age_days,
                    )
                    return None
            except ValueError:
                pass

        results = [
            {
                "date": e.get("dateEvent", ""),
                "competition": e.get("strLeague", ""),
                "home": e.get("strHomeTeam", ""),
                "away": e.get("strAwayTeam", ""),
                "score": f"{e.get('intHomeScore', '?')}–{e.get('intAwayScore', '?')}",
            }
            for e in raw[-6:]
        ]
        return {"team": team.get("strTeam", "Nigeria"), "results": results}
    except Exception as exc:
        logger.warning("TheSportsDB error: %s", exc)
        return None


# ── Sports prompt builders ────────────────────────────────────────────────────

def build_epl_standings_prompt(table):
    top5 = table[:5]
    bottom3 = table[-3:]
    top_rows = "\n".join(
        f"  {i+1}. {r['team']} — {r['points']}pts "
        f"({r['wins']}W {r['draws']}D {r['losses']}L, GF {r['gf']} GA {r['ga']})"
        for i, r in enumerate(top5)
    )
    btm_rows = "\n".join(
        f"  {len(table)-2+i}. {r['team']} — {r['points']}pts "
        f"({r['wins']}W {r['draws']}D {r['losses']}L)"
        for i, r in enumerate(bottom3)
    )
    return f"""You are a senior sports journalist at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, original HTML sports analysis article about the current Premier League standings.

CURRENT EPL TABLE (top 5):
{top_rows}

RELEGATION ZONE (bottom 3):
{btm_rows}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars>
ANALYSIS: <2–3 sentences of expert tactical/contextual analysis — what these standings reveal about the season's story and what Nigerian fans should read into it>
---
<HTML article body>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li> — no <html>/<body>/<head> tags
- <h2> headline: include the league name and current standings context
- Opening paragraph: key story from the table (title race, relegation battle, surprise package)
- <h3>Title Race</h3>: analyse the top 3 teams' chances
- <h3>Nigerian Players to Watch</h3>: mention any Nigerian players in the league and their clubs' positions (use only players you are confident exist in the EPL)
- <h3>Relegation Battle</h3>: analyse the bottom 3
- <h3>What Nigerian Fans Should Know</h3>: why this standings snapshot matters to Nigerian football fans
- Closing: a forward-looking paragraph
- Length: 500–650 words | Tone: engaging, knowledgeable, fan-friendly
- Do NOT invent scores, statistics, or player names beyond what is provided"""


def build_nigeria_results_prompt(data):
    results_text = "\n".join(
        f"  {r['date']} | {r['competition']} | {r['home']} {r['score']} {r['away']}"
        for r in data["results"]
    )
    return f"""You are a senior sports journalist at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, original HTML sports article analysing the Nigeria Super Eagles' recent results.

RECENT RESULTS:
{results_text}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars>
ANALYSIS: <2–3 sentences of expert analysis — what this run of form says about the team's prospects and what needs to change or continue>
---
<HTML article body>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li> — no <html>/<body>/<head> tags
- <h2> headline: reference the Super Eagles and the form shown by these results
- Opening paragraph: summarise the recent run of form — good, bad, or mixed?
- <h3>Match by Match</h3>: brief analysis of each result, identifying patterns
- <h3>What Is Working and What Is Not</h3>: tactical and performance observations
- <h3>Looking Ahead</h3>: what these results mean for the Super Eagles — rebuilding, upcoming fixtures, and the road to World Cup 2030 qualifying
- <h3>Fan Verdict</h3>: a balanced take on what Nigerian fans should feel — encouragement or concern?
- Closing: forward-looking sentence about upcoming fixtures or goals
- Length: 500–650 words | Tone: passionate, expert, balanced
- Do NOT invent players, managers, or scores not shown in the data above"""


# Static sports analysis topics (AI uses training knowledge — always inject current date)
SPORTS_ANALYSIS_TOPICS = [
    {
        "key": "africa_world_cup_2026_campaign",
        "title": "Africa at World Cup 2026: How CAF Nations Are Performing",
        "tags": "world cup 2026, africa, caf, morocco, senegal, football, tournament",
        "frequency_days": 7,
        "prompt": """You are a senior sports journalist at PulseLineDaily.

Write a complete, original HTML sports analysis article about African nations at the FIFA World Cup 2026.

CONTEXT: The FIFA World Cup 2026 is currently being hosted by USA, Canada, and Mexico. It features 48 teams for the first time, with CAF (Africa) allocated 9 spots. Nigeria did NOT qualify. The 9 CAF nations that qualified include Morocco, Senegal, Egypt, Ivory Coast, Cameroon, South Africa, and others. Write about this tournament as currently ongoing.

TODAY'S DATE: {date_str}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars>
ANALYSIS: <2–3 sentences of expert analysis — Africa's overall World Cup 2026 performance and whether the continent is living up to the historic 9-slot opportunity>
---
<HTML article body>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li>
- <h2>: strong headline about Africa's World Cup 2026 campaign (frame as ongoing tournament)
- Opening: how many African nations qualified and the historic 9-slot opportunity for CAF
- <h3>Africa's Strongest Contenders</h3>: Morocco, Senegal, Egypt, Ivory Coast, Cameroon, South Africa — which CAF nations have the best shot at the knockout rounds and why
- <h3>The New Round of 32</h3>: how the expanded 48-team format's Round of 32 gives African nations a better chance than the old format
- <h3>Stars to Watch</h3>: African players who could define this tournament — name only players you are confident about
- <h3>What This Means for African Football</h3>: what a strong African showing at WC2026 would mean for the continent's global standing
- Closing: a measured look at Africa's realistic chances of a deep run
- Length: 550–700 words | Tone: expert, analytical, fan-first
- Do NOT mention Nigeria as a participant — Nigeria did not qualify for World Cup 2026""",
    },
    {
        "key": "nigeria_world_cup_history",
        "title": "Nigeria at the World Cup: Every Appearance Reviewed",
        "tags": "super eagles, nigeria, world cup history, football, nwankwo kanu, jay jay okocha",
        "frequency_days": 30,
        "prompt": """You are a senior sports journalist at PulseLineDaily.

Write a complete, original HTML sports feature about Nigeria's history at the FIFA World Cup, published while World Cup 2026 is underway.

TODAY'S DATE: {date_str}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars>
ANALYSIS: <2–3 sentences reflecting on Nigeria's World Cup legacy — what the history reveals about the team's potential and what the country must learn from missing 2026>
---
<HTML article body>

REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li>
- <h2>: compelling headline about Nigeria's World Cup story, framed against the backdrop of WC2026 happening without them
- Opening: Nigeria's overall World Cup record and significance to African football — and the pain of watching WC2026 from the outside
- For each tournament Nigeria appeared in (1994, 1998, 2002, 2010, 2014, 2018): a short paragraph covering squad highlights, results, and what went wrong or right
- <h3>Greatest World Cup Moments</h3>: top 3 moments in Nigerian World Cup history
- <h3>The Players Who Defined the Stage</h3>: Jay-Jay Okocha, Nwankwo Kanu, Rashidi Yekini, Vincent Enyeama, John Obi Mikel — brief tributes
- <h3>What Went Wrong in 2026 Qualifying</h3>: an honest paragraph on why Nigeria missed out — do not fabricate specific match scores; speak in general terms about the failures
- <h3>The Road Back</h3>: what Nigeria must fix to qualify for World Cup 2030
- Length: 600–750 words | Tone: nostalgic, honest, forward-looking
- Use only established historical facts you are confident about""",
    },
    {
        "key": "world_cup_2026_global_spotlight",
        "title": "World Cup 2026: Players and Teams Every Fan Is Talking About",
        "tags": "world cup 2026, football, mbappe, vinicius, tournament, knockout stage, top teams",
        "frequency_days": 7,
        "prompt": """You are a senior sports journalist at PulseLineDaily.

Write a complete, original HTML sports analysis article about the FIFA World Cup 2026 — covering the players, teams, and storylines dominating the tournament.

CONTEXT: World Cup 2026 is currently being played in USA, Canada, and Mexico. 48 teams, new Round of 32 format. Nigeria is NOT in the tournament. Write as though the tournament is actively underway.

TODAY'S DATE: {date_str}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars>
ANALYSIS: <2–3 sentences of expert analysis — the single biggest story defining WC2026 so far>
---
<HTML article body>

REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li>
- <h2>: punchy headline about WC2026's biggest storyline
- Opening: set the scene — where the tournament stands and what has captured global attention
- <h3>The Favourites</h3>: which nations look most likely to lift the trophy — France, Brazil, England, Argentina, Spain, etc. — use only teams you are confident about
- <h3>The Stars Shining Brightest</h3>: 4–5 players defining the tournament (Mbappe, Vinicius Jr, Bellingham, Saka, and others) — name only players you are confident are at the tournament
- <h3>The Surprise Packages</h3>: teams that have outperformed expectations in this expanded 48-team field
- <h3>African Nations at the Tournament</h3>: how Morocco, Senegal, Egypt, and other African nations are faring — do NOT include Nigeria
- Closing: what the remaining knockout rounds promise for football fans
- Length: 550–700 words | Tone: energetic, expert, global football fan voice
- Do NOT fabricate specific June 2026 match scores you cannot verify — speak in analytical/assessment terms""",
    },
]


def build_static_sports_prompt(topic, date_str=""):
    """Return the topic's prompt with the current date injected."""
    return topic["prompt"].replace("{date_str}", date_str)


# ── World Cup 2026 live data ──────────────────────────────────────────────────

def fetch_world_cup_2026_data():
    """
    Fetch live FIFA World Cup 2026 group standings and recent match scores from ESPN.
    Uses the same public ESPN API as fetch_epl_standings().
    Returns a dict with 'groups' and/or 'matches', or None if both fail.
    """
    base = "https://site.api.espn.com/apis/v2/sports/soccer/fifa.world"
    result = {}

    # Group standings
    try:
        resp = requests.get(f"{base}/standings", timeout=12)
        resp.raise_for_status()
        data = resp.json()
        groups = []
        for group in (data.get("children") or []):
            group_name = group.get("name", "")
            entries = []
            for entry in group.get("standings", {}).get("entries", []):
                name = entry.get("team", {}).get("displayName", "Unknown")
                stats = {s["name"]: s["value"] for s in entry.get("stats", [])}
                entries.append({
                    "team": name,
                    "played": int(stats.get("gamesPlayed", 0)),
                    "points": int(stats.get("points", 0)),
                    "wins": int(stats.get("wins", 0)),
                    "draws": int(stats.get("ties", 0)),
                    "losses": int(stats.get("losses", 0)),
                    "gf": int(stats.get("pointsFor", 0)),
                    "ga": int(stats.get("pointsAgainst", 0)),
                })
            if entries:
                groups.append({"group": group_name, "teams": sorted(entries, key=lambda x: x["points"], reverse=True)})
        if groups:
            result["groups"] = groups
    except Exception as exc:
        logger.warning("ESPN WC2026 standings error: %s", exc)

    # Recent/live match scores from the scoreboard
    try:
        resp = requests.get(f"{base}/scoreboard", timeout=12)
        resp.raise_for_status()
        data = resp.json()
        matches = []
        for event in (data.get("events") or [])[:30]:
            comps = event.get("competitions", [{}])
            comp = comps[0] if comps else {}
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue
            home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
            away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
            status_type = event.get("status", {}).get("type", {})
            note = comp.get("notes", [{}])
            stage = note[0].get("headline", "") if note else ""
            matches.append({
                "date": event.get("date", "")[:10],
                "home": home.get("team", {}).get("displayName", ""),
                "away": away.get("team", {}).get("displayName", ""),
                "home_score": home.get("score", ""),
                "away_score": away.get("score", ""),
                "completed": status_type.get("completed", False),
                "stage": stage,
            })
        if matches:
            result["matches"] = matches
    except Exception as exc:
        logger.warning("ESPN WC2026 scoreboard error: %s", exc)

    return result if result else None


def build_world_cup_2026_prompt(data, date_str):
    """
    Build a prompt to write a World Cup 2026 article using live ESPN data.
    """
    # Format group standings — Nigeria's group first if found, then a sample of others
    groups_text = ""
    if data.get("groups"):
        nigeria_groups = [g for g in data["groups"] if any("nigeria" in t["team"].lower() for t in g["teams"])]
        other_groups = [g for g in data["groups"] if g not in nigeria_groups]
        show_groups = nigeria_groups + other_groups[:4]
        lines = []
        for g in show_groups:
            lines.append(f"  {g['group']}:")
            for t in g["teams"]:
                lines.append(
                    f"    {t['team']} — {t['points']}pts "
                    f"({t['wins']}W {t['draws']}D {t['losses']}L, "
                    f"GF {t['gf']} GA {t['ga']}, Played {t['played']})"
                )
        groups_text = "\n".join(lines)
    else:
        groups_text = "(Group standings unavailable — write based on tournament context)"

    # Format recent matches
    matches_text = ""
    if data.get("matches"):
        completed = [m for m in data["matches"] if m["completed"]]
        upcoming = [m for m in data["matches"] if not m["completed"]]
        lines = []
        if completed:
            lines.append("  Recent results:")
            for m in completed[-10:]:
                stage = f" [{m['stage']}]" if m["stage"] else ""
                lines.append(f"    {m['date']}{stage}: {m['home']} {m['home_score']}–{m['away_score']} {m['away']}")
        if upcoming:
            lines.append("  Upcoming fixtures:")
            for m in upcoming[:6]:
                stage = f" [{m['stage']}]" if m["stage"] else ""
                lines.append(f"    {m['date']}{stage}: {m['home']} vs {m['away']}")
        matches_text = "\n".join(lines)
    else:
        matches_text = "(Live match data unavailable)"

    return f"""You are a senior sports journalist at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, original HTML article covering the FIFA World Cup 2026 — the current state of the tournament and what it means for African football and global football fans.

IMPORTANT: Nigeria did NOT qualify for World Cup 2026. Do NOT write about Nigeria as a tournament participant. Focus on the nations that are there.

TODAY: {date_str}
TOURNAMENT: FIFA World Cup 2026 (USA, Canada, Mexico) — 48 teams, new Round of 32 knockout format

LIVE STANDINGS DATA:
{groups_text}

LIVE MATCH DATA:
{matches_text}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars — include a specific result or standings fact from the data above>
ANALYSIS: <2–3 sentences of expert analysis — the most important development in the tournament right now>
---
<full HTML article body starting with <h2>>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li> — no <html>/<body>/<head> tags
- <h2>: headline naming a specific result, standing, or match — must reference World Cup 2026
- Opening paragraph: the current state of the tournament — what stage we are at, the biggest storyline from the data above
- <h3>Group Standings Snapshot</h3>: highlight the most interesting groups from the data — who is topping, who is on the edge, any surprise standings
- <h3>Africa at the World Cup</h3>: how the CAF nations (Morocco, Senegal, Egypt, Ivory Coast, Cameroon, South Africa, etc.) are performing based on the standings data — who is advancing, who is struggling
- <h3>Matches to Watch</h3>: upcoming fixtures of note from the data, and which games could define the Round of 32
- <h3>Tournament Talking Points</h3>: the biggest stories — surprise results, dominant teams, star performers — based strictly on the data provided
- Closing: a forward-looking paragraph about what the next stage holds
- Length: 550–700 words | Tone: energetic, expert, global football audience
- Attribute data to ESPN/FIFA
- Do NOT invent match scores or standings figures beyond the data provided above"""


# ── WC2026 match preview ──────────────────────────────────────────────────────

def fetch_wc2026_fixtures(date_str=None):
    """
    Fetch today's (or a specific date's) WC2026 fixtures from ESPN.
    date_str format: 'YYYYMMDD' — omit for today.
    Returns list of match dicts or None on failure.
    """
    params = {"dates": date_str} if date_str else {}
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/v2/sports/soccer/fifa.world/scoreboard",
            params=params,
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()
        fixtures = []
        for event in (data.get("events") or []):
            comp = (event.get("competitions") or [{}])[0]
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue
            home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
            away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
            status = event.get("status", {}).get("type", {})
            note = (comp.get("notes") or [{}])[0]
            fixtures.append({
                "datetime": event.get("date", ""),
                "home": home.get("team", {}).get("displayName", ""),
                "away": away.get("team", {}).get("displayName", ""),
                "home_score": home.get("score", ""),
                "away_score": away.get("score", ""),
                "completed": status.get("completed", False),
                "in_progress": status.get("name", "") in ("in", "halftime"),
                "stage": note.get("headline", ""),
                "venue": comp.get("venue", {}).get("fullName", ""),
            })
        return fixtures or None
    except Exception as exc:
        logger.warning("ESPN WC2026 fixtures error: %s", exc)
        return None


def build_wc2026_match_preview_prompt(fixtures, date_str):
    today = [f for f in fixtures if not f["completed"]]
    done = [f for f in fixtures if f["completed"]]

    preview_lines = "\n".join(
        f"  {f['home']} vs {f['away']}"
        + (f" [{f['stage']}]" if f["stage"] else "")
        + (f" @ {f['venue']}" if f["venue"] else "")
        for f in today
    ) or "  (No upcoming fixtures found for today)"

    results_lines = "\n".join(
        f"  {f['home']} {f['home_score']}–{f['away_score']} {f['away']}"
        + (f" [{f['stage']}]" if f["stage"] else "")
        for f in done[-8:]
    ) or "  (No completed results today)"

    return f"""You are a senior sports journalist at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, original HTML match preview article for today's FIFA World Cup 2026 fixtures.

TODAY: {date_str}
NIGERIA IS NOT AT THIS WORLD CUP — do not refer to them as participants.

TODAY'S UPCOMING MATCHES:
{preview_lines}

TODAY'S COMPLETED RESULTS (for context):
{results_lines}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars — name the most exciting match today>
ANALYSIS: <2–3 sentences on what today's fixtures mean for the tournament picture>
---
<full HTML article body starting with <h2>>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li> — no <html>/<body>/<head> tags
- <h2>: headline naming today's key fixture(s) and the World Cup 2026
- Opening paragraph: set the scene — what today's matches mean for who advances
- For each upcoming match, write a short preview paragraph covering: both teams' recent form, key players to watch, what's at stake (qualification, top of group, etc.)
- <h3>African Teams in Action</h3>: highlight any CAF nations playing today and their situation — skip this section if no African teams play today
- <h3>Ones to Watch Today</h3>: 2–3 individual players who could be decisive across today's fixtures
- <h3>Predictions</h3>: a brief, reasoned prediction for each match — use analytical language, not invented scores
- Closing: what today's results could mean for the Round of 32 picture
- Length: 550–700 words | Tone: excited, expert, football-fan voice
- Do NOT invent specific scores for matches listed as upcoming
- Attribute data to ESPN/FIFA"""


# ── WC2026 golden boot / top scorers ─────────────────────────────────────────

def fetch_wc2026_top_scorers():
    """
    Fetch WC2026 top scorers from ESPN's leaders endpoint.
    Returns a list of {name, country, goals} dicts or None on failure.
    """
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/v2/sports/soccer/fifa.world/leaders",
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()
        scorers = []
        for cat in (data.get("categories") or []):
            if "goal" not in (cat.get("name") or "").lower() and \
               "goal" not in (cat.get("displayName") or "").lower():
                continue
            for leader in (cat.get("leaders") or [])[:10]:
                athlete = leader.get("athlete") or leader.get("statistics", {})
                name = (
                    athlete.get("displayName")
                    or athlete.get("shortName")
                    or leader.get("displayName", "Unknown")
                )
                country = (
                    (leader.get("team") or {}).get("displayName")
                    or (athlete.get("team") or {}).get("displayName", "")
                )
                goals = int(leader.get("value", 0))
                if name and goals > 0:
                    scorers.append({"name": name, "country": country, "goals": goals})
            if scorers:
                break
        return scorers or None
    except Exception as exc:
        logger.warning("ESPN WC2026 top scorers error: %s", exc)
        return None


def build_wc2026_golden_boot_prompt(scorers, date_str):
    scorer_lines = "\n".join(
        f"  {i+1}. {s['name']} ({s['country']}) — {s['goals']} goal{'s' if s['goals'] != 1 else ''}"
        for i, s in enumerate(scorers)
    )
    return f"""You are a senior sports journalist at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, original HTML article about the FIFA World Cup 2026 Golden Boot race — who is leading, who could challenge, and what it means for the tournament.

TODAY: {date_str}
NIGERIA IS NOT AT THIS WORLD CUP — do not refer to them as participants.

CURRENT TOP SCORERS (live data from ESPN/FIFA):
{scorer_lines}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars — name the current Golden Boot leader and their tally>
ANALYSIS: <2–3 sentences on what this scoring race reveals about the tournament's attacking quality>
---
<full HTML article body starting with <h2>>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li> — no <html>/<body>/<head> tags
- <h2>: headline naming the Golden Boot leader and their goal tally
- Opening paragraph: introduce the Golden Boot race and name the current leader with their goals
- <h3>The Race So Far</h3>: analyse the top 5 scorers — their style, their team's campaign, why they're scoring
- <h3>Who Could Challenge</h3>: players just behind the leader who could overtake in the knockout rounds
- <h3>African Strikers in the Race</h3>: highlight any African players in the top scorers — skip this section if none appear in the data above
- <h3>Historic Context</h3>: how does this scoring pace compare to past World Cup Golden Boot winners?
- Closing: who looks most likely to lift the Golden Boot by the final
- Length: 500–650 words | Tone: analytical, engaging, stat-driven
- Attribute data to ESPN/FIFA
- Do NOT invent goal tallies beyond the data above"""


# ── Yahoo Finance helper ──────────────────────────────────────────────────────

_YF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def fetch_yahoo_price(ticker):
    """
    Fetch the latest price for a Yahoo Finance ticker (commodity futures or index).
    Returns {price, prev_close, change_pct, currency, name} or None on failure.
    """
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
    try:
        resp = requests.get(
            url,
            params={"interval": "1d", "range": "5d"},
            headers=_YF_HEADERS,
            timeout=12,
        )
        resp.raise_for_status()
        results = resp.json()["chart"]["result"]
        if not results:
            return None
        meta = results[0]["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if not price or not prev:
            return None
        change_pct = round(((float(price) - float(prev)) / float(prev)) * 100, 2)
        return {
            "price": round(float(price), 2),
            "prev_close": round(float(prev), 2),
            "change_pct": change_pct,
            "currency": meta.get("currency", "USD"),
            "name": meta.get("longName") or meta.get("shortName", ticker),
        }
    except Exception as exc:
        logger.warning("Yahoo Finance [%s] error: %s", ticker, exc)
        return None


# ── Oil & energy fetcher ──────────────────────────────────────────────────────

def fetch_brent_crude():
    """
    Return live Brent and WTI crude prices from Yahoo Finance, or None on failure.
    Brent ticker: BZ=F  |  WTI ticker: CL=F
    """
    brent = fetch_yahoo_price("BZ=F")
    if not brent:
        return None
    wti = fetch_yahoo_price("CL=F")
    return {
        "brent_price": brent["price"],
        "brent_prev": brent["prev_close"],
        "brent_change_pct": brent["change_pct"],
        "wti_price": wti["price"] if wti else None,
        "currency": "USD",
    }


# ── NGX stock market fetcher ──────────────────────────────────────────────────

def fetch_ngx_index():
    """
    Return the NGX All-Share Index from Yahoo Finance (ticker ^NGSEINDEX), or None.
    """
    data = fetch_yahoo_price("^NGSEINDEX")
    if not data:
        return None
    return {
        "value": data["price"],
        "prev_close": data["prev_close"],
        "change_pct": data["change_pct"],
    }


# ── Crypto prices ────────────────────────────────────────────────────────────

def fetch_crypto_prices():
    """
    Fetch live Bitcoin and Ethereum prices from Yahoo Finance.
    Returns {btc, eth} dict (each may be None) or None if both fail.
    """
    btc = fetch_yahoo_price("BTC-USD")
    eth = fetch_yahoo_price("ETH-USD")
    if not btc and not eth:
        return None
    return {"btc": btc, "eth": eth}


def build_crypto_prompt(data, date_str, ngn_rate=None):
    lines = []
    btc_ngn = eth_ngn = None

    if data.get("btc"):
        b = data["btc"]
        sign = "+" if b["change_pct"] >= 0 else ""
        lines.append(
            f"- Bitcoin (BTC): ${b['price']:,.2f} USD  ({sign}{b['change_pct']}% today)"
        )
        if ngn_rate:
            btc_ngn = b["price"] * ngn_rate

    if data.get("eth"):
        e = data["eth"]
        sign = "+" if e["change_pct"] >= 0 else ""
        lines.append(
            f"- Ethereum (ETH): ${e['price']:,.2f} USD  ({sign}{e['change_pct']}% today)"
        )
        if ngn_rate:
            eth_ngn = e["price"] * ngn_rate

    prices_text = "\n".join(lines)

    naira_block = ""
    if ngn_rate:
        naira_block = f"\nLIVE USD/NGN RATE (use this — do NOT guess the exchange rate): 1 USD = ₦{ngn_rate:,.2f}"
        if btc_ngn:
            naira_block += f"\n- 1 BTC ≈ ₦{btc_ngn:,.0f}"
        if eth_ngn:
            naira_block += f"\n- 1 ETH ≈ ₦{eth_ngn:,.0f}"
    else:
        naira_block = "\nNaira equivalent: do NOT guess the USD/NGN exchange rate — omit specific Naira figures if the rate is unavailable."

    return f"""You are a senior financial and technology journalist at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, publish-ready HTML article about today's Bitcoin and Ethereum prices and their significance for Nigerian crypto investors.

TODAY: {date_str}

LIVE CRYPTO PRICES (Yahoo Finance):
{prices_text}
{naira_block}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <one punchy sentence max 160 characters — include BTC price and direction>
ANALYSIS: <2–3 sentences of expert takeaway — what today's crypto prices mean for Nigerian holders and the broader digital asset market>
---
<full HTML article body starting with <h2>>

ARTICLE REQUIREMENTS:
- Pure HTML — every section title MUST be wrapped in <h3>...</h3> tags, every paragraph in <p>...</p>, every list in <ul><li>...</li></ul>
- Do NOT include <html>, <body>, <head>, or <style> tags
- Do NOT use plain bold text as section headers — use <h3> tags only
- <h2>: punchy headline naming the BTC price, percentage change, and date
- <p> Opening: state both prices and their daily changes; use the Naira equivalents provided above to give local context — do NOT invent your own exchange rate
- <h3>What This Means for Nigerian Investors</h3>: <p> explaining impact on local holders, P2P traders, and remittance users; reference the Naira equivalents from the data above
- <h3>Market Drivers</h3>: <p> covering macro factors (US economic data, institutional moves, global risk sentiment) — do NOT fabricate specific news events
- <h3>Bitcoin vs Ethereum</h3>: <p> comparing both assets' daily performance and what the divergence signals about market sentiment
- <h3>What Should Nigerian Crypto Users Do?</h3>: <ul> with 4 <li> practical tips — holding strategy, risk management, staying informed, security practices
- <h3>Regulatory Watch</h3>: <p> on Nigeria's crypto regulatory environment (SEC Nigeria, CBN stance) — use only established facts
- Closing <p>: a sharp, specific final observation about what today's prices mean for Nigeria's crypto future — do NOT open with "In conclusion"
- Attribute data to Yahoo Finance
- Length: 550–700 words | Tone: informative, practical, balanced — not hype
- SEO: "bitcoin price today nigeria", "ethereum price naira", "crypto nigeria", "btc today"
- Do NOT fabricate specific regulatory announcements, named quotes, or exchange rates not provided above"""


# ── Agriculture commodity fetchers ────────────────────────────────────────────

def fetch_commodity_prices():
    """
    Return current prices for cocoa (Yahoo Finance), palm oil, and groundnuts
    (World Bank Pink Sheet).  Returns a dict with whichever sources succeed.
    Returns None if every source fails.
    """
    result = {}

    cocoa = fetch_yahoo_price("CC=F")
    if cocoa:
        result["cocoa"] = {
            "price": cocoa["price"],
            "change_pct": cocoa["change_pct"],
            "unit": "USD/tonne",
            "source": "ICE Futures",
        }

    palm_pts = fetch_worldbank_indicator("PPALMOIL", country="WLD", mrv=6)
    if palm_pts:
        result["palm_oil"] = {
            "price": palm_pts[-1]["value"],
            "year": palm_pts[-1]["year"],
            "unit": "USD/tonne",
            "source": "World Bank",
        }

    gnut_pts = fetch_worldbank_indicator("PGNUTS", country="WLD", mrv=6)
    if gnut_pts:
        result["groundnuts"] = {
            "price": gnut_pts[-1]["value"],
            "year": gnut_pts[-1]["year"],
            "unit": "USD/tonne",
            "source": "World Bank",
        }

    return result or None


# ── ACLED security fetcher ────────────────────────────────────────────────────

def fetch_acled_nigeria(api_key, email, days=30):
    """
    Fetch recent conflict/security incidents for Nigeria from the ACLED API.
    Free account required at acleddata.com — set ACLED_API_KEY and ACLED_EMAIL in .env.
    Returns a summary dict or None on failure.
    """
    from collections import Counter
    from datetime import datetime, timedelta, timezone as dt_timezone

    now_utc = datetime.now(dt_timezone.utc)
    since = (now_utc - timedelta(days=days)).strftime("%Y-%m-%d")
    today = now_utc.strftime("%Y-%m-%d")
    try:
        resp = requests.get(
            "https://api.acleddata.com/acled/read",
            params={
                "key": api_key,
                "email": email,
                "country": "Nigeria",
                "event_date": since,
                "event_date_where": "BETWEEN",
                "event_date2": today,
                "limit": 500,
            },
            timeout=20,
        )
        resp.raise_for_status()
        events = resp.json().get("data") or []
        if not events:
            return None

        fatalities = sum(int(e.get("fatalities") or 0) for e in events)
        regions = Counter(e.get("admin1", "Unknown") for e in events)
        event_types = Counter(e.get("event_type", "Unknown") for e in events)

        return {
            "total_events": len(events),
            "fatalities": fatalities,
            "days": days,
            "top_regions": regions.most_common(5),
            "event_types": dict(event_types.most_common(5)),
            "since_date": since,
        }
    except Exception as exc:
        logger.warning("ACLED error: %s", exc)
        return None


# ── Market & security prompt builders ────────────────────────────────────────

def build_brent_prompt(data, date_str=""):
    brent = data["brent_price"]
    change = data["brent_change_pct"]
    direction = "rose" if change >= 0 else "fell"
    sign = "+" if change >= 0 else ""
    wti_line = (
        f"\n- WTI Crude (US benchmark): ${data['wti_price']:.2f}/barrel"
        if data.get("wti_price") else ""
    )
    return f"""You are a senior energy and financial journalist at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, publish-ready HTML article about today's Brent crude oil price and its implications for Nigeria.

TODAY: {date_str}

DATA (live market — use these exact figures):
- Brent Crude (global benchmark): ${brent:.2f}/barrel{wti_line}
- Change today: {sign}{change}% — price {direction}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <one punchy sentence max 160 characters — include the exact Brent price and direction>
ANALYSIS: <2–3 sentences of expert takeaway — what this price signals about global oil markets and Nigeria's fiscal position>
---
<full HTML article body starting with <h2>>

ARTICLE REQUIREMENTS:
- Pure HTML — every section title MUST be in <h3>...</h3> tags, every paragraph in <p>...</p>, every list in <ul><li>...</li></ul>
- Do NOT include <html>, <body>, <head>, or <style> tags
- Do NOT use plain bold text as section headers — use <h3> tags only
- <h2>: headline naming the exact Brent price (${brent:.2f}/barrel), the daily movement, and the date
- <p> Opening: state the price, the exact daily change ({sign}{change}%), and why today's level matters to Nigeria specifically
- <h3>What This Means for Nigeria's Budget</h3>: <p> explaining Nigeria's crude benchmark in the national budget and the fiscal gap or surplus created by today's price
- <h3>Impact on the Naira and Petrol Prices</h3>: <p> on how this crude price affects forex earnings, naira stability, and domestic fuel costs for Nigerians
- <h3>OPEC and Nigeria's Production</h3>: <p> on OPEC+ quota dynamics and Nigeria's output trajectory — do NOT fabricate specific barrel-per-day figures
- <h3>Outlook</h3>: <p> with a balanced forward look at oil market drivers (geopolitics, OPEC+ decisions, global demand)
- Closing <p>: a sharp, specific sentence on what Nigerian policymakers and citizens should watch next — do NOT open with "In conclusion"
- Attribute data to Yahoo Finance / live market data
- Length: 500–650 words | Tone: expert, financial, accessible
- SEO: "brent crude price today", "nigeria oil price", "opec nigeria", "naira oil revenue"
- Do NOT fabricate specific policy announcements, named quotes, or barrel-per-day production figures"""


def build_ngx_prompt(data, date_str):
    value = data["value"]
    change = data["change_pct"]
    direction = "gained" if change >= 0 else "fell"
    sign = "+" if change >= 0 else ""
    return f"""You are a senior financial markets journalist at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, publish-ready HTML article about the NGX All-Share Index performance.

DATA (NGX market):
- NGX All-Share Index: {value:,.2f} points
- Daily change: {sign}{change}% — market {direction}
- Date: {date_str}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <one punchy sentence max 160 characters — include the index level and direction>
ANALYSIS: <2–3 sentences of expert analysis — what is driving today's market and what investors should watch>
---
<full HTML article body starting with <h2>>

ARTICLE REQUIREMENTS:
- Pure HTML — every section title MUST be in <h3>...</h3> tags, every paragraph in <p>...</p>, every list in <ul><li>...</li></ul>
- Do NOT include <html>, <body>, <head>, or <style> tags
- Do NOT use plain bold text as section headers — use <h3> tags only
- <h2>: headline naming the exact index level ({value:,.2f}), direction, and date
- <p> Opening: state the index level, the exact daily change ({sign}{change}%), and the market tone for the day
- <h3>What's Driving the Market</h3>: <p> on macro factors (FX stability, oil price, interest rates, investor sentiment) — do NOT fabricate specific stock prices or volumes
- <h3>Sectors in Focus</h3>: <p> on which sectors are moving Nigerian equities (banking, telecoms, FMCG, cement) — use general knowledge, not invented figures
- <h3>What Nigerian Investors Should Know</h3>: <ul> with 4–5 <li> practical takeaways for retail and institutional investors
- <h3>Outlook</h3>: <p> with a balanced forward-looking view on Nigerian equities
- Closing <p>: a specific, actionable sentence for Nigerian investors — do NOT open with "In conclusion"
- Attribute data to NGX / Yahoo Finance
- Length: 500–600 words | Tone: professional, financial, accessible to retail investors
- SEO: "ngx all share index today", "nigerian stock market", "ngx market wrap"
- Do NOT invent specific stock tickers, price moves, or named quotes"""


def build_commodity_prompt(data):
    lines = []
    if "cocoa" in data:
        c = data["cocoa"]
        sign = "+" if c["change_pct"] >= 0 else ""
        lines.append(
            f"- Cocoa: ${c['price']:,.2f}/{c['unit']} "
            f"({sign}{c['change_pct']}% today) — {c['source']}"
        )
    if "palm_oil" in data:
        p = data["palm_oil"]
        lines.append(
            f"- Palm Oil: ${p['price']:,.2f}/{p['unit']} ({p['year']} latest) — {p['source']}"
        )
    if "groundnuts" in data:
        g = data["groundnuts"]
        lines.append(
            f"- Groundnuts: ${g['price']:,.2f}/{g['unit']} ({g['year']} latest) — {g['source']}"
        )
    prices_text = "\n".join(lines) if lines else "Partial price data only — use general market context."

    return f"""You are a senior agricultural economics journalist at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, publish-ready HTML article analysing current prices for key Nigerian agricultural export commodities.

DATA (current market prices):
{prices_text}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <one punchy sentence max 160 characters — name the key commodity price story>
ANALYSIS: <2–3 sentences of expert analysis — what these prices mean for Nigerian farmers, exporters, and the national economy>
---
<full HTML article body starting with <h2>>

ARTICLE REQUIREMENTS:
- Pure HTML — every section title MUST be in <h3>...</h3> tags, every paragraph in <p>...</p>, every list in <ul><li>...</li></ul>
- Do NOT include <html>, <body>, <head>, or <style> tags
- Do NOT use plain bold text as section headers — use <h3> tags only
- <h2>: headline naming the most newsworthy commodity price from the data above and the period
- <p> Opening: lead with the single most newsworthy price figure from the data and its direct significance for Nigeria
- <h3>Cocoa Prices and Nigerian Farmers</h3>: <p> Nigeria is Africa's 4th-largest cocoa producer — what does the price mean for farmers in Ondo, Osun, Cross River?
- <h3>Palm Oil Market</h3>: <p> price impact on farmers, processors, and local consumers
- <h3>Groundnuts and Northern Agriculture</h3>: <p> importance to Kano, Kaduna, Katsina farmers and export earnings
- <h3>What Government and Exporters Should Do</h3>: <ul> with 3–4 <li> policy and business recommendations
- <h3>Outlook</h3>: <p> general market direction for agricultural commodities
- Closing <p>: a sharp sentence on what Nigerian farmers and traders should act on now — do NOT open with "In conclusion"
- Attribute cocoa data to ICE Futures; palm oil and groundnut data to World Bank
- Length: 550–700 words | Tone: expert, accessible, pro-farmer
- SEO: "nigeria cocoa price", "palm oil price nigeria", "groundnut price nigeria"
- Do NOT fabricate local naira farm-gate prices or invent named quotes"""


def build_acled_prompt(data):
    event_lines = "\n".join(
        f"  - {etype}: {count} incidents"
        for etype, count in data["event_types"].items()
    )
    region_lines = "\n".join(
        f"  - {region}: {count} events"
        for region, count in data["top_regions"]
    )
    return f"""You are a senior security analyst and journalist at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, publish-ready HTML security briefing about the current security situation in Nigeria, based on verified conflict data.

DATA (ACLED conflict database — last {data['days']} days, from {data['since_date']}):
- Total security incidents recorded: {data['total_events']}
- Total recorded fatalities: {data['fatalities']}

Incident breakdown by type:
{event_lines}

Most affected states:
{region_lines}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <one punchy sentence max 160 characters — name the key security trend>
ANALYSIS: <2–3 sentences of expert analysis — the overall trend and what policymakers must act on most urgently>
---
<full HTML article body starting with <h2>>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li> — no <html>/<body>/<head> tags
- <h2>: headline naming the key security concern and timeframe
- Paragraph 1: state the headline numbers (incidents, fatalities) and overall security picture
- <h3>Hotspot States</h3>: discuss the most affected states and the nature of insecurity there
- <h3>Types of Incidents</h3>: explain what the incident categories reveal about the nature of violence
- <h3>Impact on Civilians and Communities</h3>: displacement, economic disruption, humanitarian needs
- <h3>Government and Military Response</h3>: general context on counter-insurgency — do NOT fabricate specific operation names or casualty figures beyond the data
- <h3>What Must Change</h3>: 3–4 evidence-based recommendations
- Attribute data clearly to ACLED (Armed Conflict Location & Event Data Project)
- Length: 600–750 words | Tone: serious, factual, human-centered, constructive
- SEO: "nigeria security", "conflict nigeria", "insecurity nigeria", "banditry terrorism nigeria"
- Do NOT sensationalise or attribute attacks to specific groups without clear data evidence"""


# ── Nigeria Politics analysis topics ─────────────────────────────────────────

POLITICS_ANALYSIS_TOPICS = [
    {
        "key": "nigeria_2027_elections",
        "title": "Nigeria 2027 General Elections: Early Analysis",
        "tags": "nigeria, 2027 elections, inec, apc, pdp, labour party, politics, voting",
        "frequency_days": 7,
        "prompt": """You are a senior political analyst and journalist at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, original HTML analysis article about Nigeria's 2027 general elections.

TODAY'S DATE: {date_str}

CRITICAL — KNOWLEDGE CUTOFF WARNING: Your training data ends around August 2025. Today is {date_str}, meaning roughly 10 months have passed since your cutoff. Events that were "upcoming" in your training knowledge — such as party primaries, candidate declarations, INEC registration drives, and court rulings — may have already occurred or been resolved. Do NOT write about these as future events. Instead, write about structural factors, established political dynamics, and what the election outcome will hinge on. Avoid any specific predictions about things that may have changed since August 2025.

CONTEXT: The 2027 Nigerian general elections are scheduled for February 2027. President Bola Tinubu (APC) is the incumbent. The opposition includes PDP, Labour Party, NNPP, and others. INEC is the electoral body.

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars>
ANALYSIS: <2–3 sentences of expert analysis — the single biggest factor that will determine the 2027 outcome and what Nigerian voters should watch>
---
<HTML article body>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li> — no <html>/<body>/<head> tags
- <h2>: strong headline about the 2027 elections outlook, referencing today's perspective ({date_str})
- Opening: why the 2027 elections matter for Nigeria's democratic trajectory — frame this as analysis written in {date_str}
- <h3>The Incumbent's Position</h3>: Tinubu and APC's standing based on established facts up to your knowledge cutoff — acknowledge that the political landscape is evolving
- <h3>Opposition Landscape</h3>: PDP, Labour Party, and other parties' structural prospects — focus on party strengths, weaknesses, and historical patterns rather than recent events you cannot verify
- <h3>Key Issues Voters Care About</h3>: economy, security, cost of living, subsidy removal fallout, education — structural issues that will drive voter decisions regardless of what has happened recently
- <h3>INEC and Electoral Integrity</h3>: lessons from 2023, the role of technology (BVAS, IReV), and what a credible 2027 process requires
- <h3>What Will Decide 2027</h3>: 3–4 structural factors — economic performance, opposition unity, voter turnout, regional bloc dynamics — that analysts agree will shape the outcome
- Closing: a measured, evidence-based forecast paragraph
- Length: 600–750 words | Tone: analytical, balanced, non-partisan
- Do NOT write about primaries, candidate lists, or specific alliances as future events — these processes may have already concluded
- Do NOT fabricate polling numbers, specific defections, or court outcomes from after August 2025""",
    },
    {
        "key": "tinubu_policy_scorecard",
        "title": "Tinubu Administration: Policy Scorecard",
        "tags": "tinubu, nigeria, apc, fuel subsidy, economy, policy, presidency, governance",
        "frequency_days": 14,
        "prompt": """You are a senior political and economic analyst at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, original HTML analysis article assessing the Tinubu administration's major policy decisions and their impact on Nigerians.

TODAY'S DATE: {date_str}

CONTEXT: President Bola Tinubu took office on 29 May 2023. Key decisions include removal of petrol subsidy (Day 1), unification of the naira exchange rate, tax reform bills, and security initiatives. These have had significant economic consequences for ordinary Nigerians. Your training data ends around August 2025 — focus on established policy impacts and structural analysis rather than specific events you cannot verify after that date.

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars>
ANALYSIS: <2–3 sentences of expert analysis — the single most consequential policy decision so far and whether the administration is on the right track>
---
<HTML article body>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li> — no <html>/<body>/<head> tags
- <h2>: headline assessing the Tinubu government's performance
- Opening: the overall state of governance under Tinubu — promise vs reality
- <h3>Economic Policies</h3>: subsidy removal, forex unification, naira depreciation, inflation — what worked, what hurt ordinary Nigerians
- <h3>Security Initiatives</h3>: state of insecurity across regions — progress or deterioration?
- <h3>Social Investment and Welfare</h3>: palliatives, student loans, cash transfers — are they reaching Nigerians?
- <h3>Legislative Agenda</h3>: key bills passed or pending — tax reform, electricity act, other landmark legislation
- <h3>Verdict</h3>: a balanced, evidence-based scorecard — what grade would an independent analyst give?
- Closing: what the next 12 months must deliver for Nigerians to see progress
- Length: 650–800 words | Tone: analytical, balanced, fact-based, pro-Nigerian-citizen
- Use only established facts; do NOT fabricate specific statistics beyond what is widely reported""",
    },
    {
        "key": "national_assembly_watch",
        "title": "Nigeria's National Assembly: Legislative Highlights",
        "tags": "national assembly, senate, house of representatives, nigeria, legislation, politics",
        "frequency_days": 10,
        "prompt": """You are a senior political correspondent at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, original HTML article covering the key activities and priorities of Nigeria's National Assembly.

TODAY'S DATE: {date_str}

CONTEXT: Nigeria's National Assembly comprises the Senate (109 senators) and House of Representatives (360 members). The current assembly was inaugurated in June 2023. Senate President is Godswill Akpabio; Speaker of the House is Tajudeen Abbas. Your training data ends around August 2025 — write about structural legislative roles and established bills; do NOT present pending legislation from your training as still pending if it may have been resolved.

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars>
ANALYSIS: <2–3 sentences of expert analysis — the most consequential bill or legislative development Nigerians should be paying attention to>
---
<HTML article body>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li> — no <html>/<body>/<head> tags
- <h2>: headline about the National Assembly's current legislative agenda
- Opening: the role of the National Assembly in Nigeria's democracy and current political climate
- <h3>Key Bills Under Consideration</h3>: tax reform, budget, security legislation, electoral act amendments — use only bills you are confident are real
- <h3>Oversight and Accountability</h3>: committee investigations, ministerial screenings, government accountability hearings
- <h3>Executive-Legislature Relations</h3>: areas of cooperation and tension between the presidency and the legislature
- <h3>What Nigerians Should Know</h3>: how current legislative activities affect ordinary citizens — taxes, services, rights
- Closing: what to watch in the coming legislative session
- Length: 550–700 words | Tone: informative, accessible, accountability-focused
- Use only established facts about the National Assembly; do NOT fabricate specific bill numbers or votes""",
    },
    {
        "key": "state_politics_nigeria",
        "title": "Nigerian State Politics: Key Governors and Subnational Trends",
        "tags": "governors, state politics, nigeria, subnational, apc, pdp, governance",
        "frequency_days": 21,
        "prompt": """You are a senior political analyst at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, original HTML analysis article about subnational politics in Nigeria — focusing on key state governors and the political dynamics shaping Nigeria's 36 states.

TODAY'S DATE: {date_str}

KNOWLEDGE CUTOFF NOTE: Your training ends around August 2025. Only name governors you are confident hold office as of that date. State that governorship details may have evolved since your training cutoff.

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars>
ANALYSIS: <2–3 sentences of expert analysis — the single most important subnational political trend shaping Nigeria's national politics>
---
<HTML article body>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li> — no <html>/<body>/<head> tags
- <h2>: headline about Nigerian state-level political dynamics
- Opening: why subnational politics matters as much as federal politics in Nigeria
- <h3>Governors to Watch</h3>: 4–5 state governors making significant political or policy moves — use only governors you are confident about
- <h3>APC vs PDP in the States</h3>: current state-level party distribution and what it signals for 2027
- <h3>State-Level Governance Issues</h3>: local government autonomy, state IGR, infrastructure delivery, security at state level
- <h3>Rising Political Figures</h3>: deputy governors, speakers, commissioners making noise — the next generation of leaders
- Closing: how state-level dynamics will shape the 2027 presidential contest
- Length: 550–700 words | Tone: analytical, insightful, non-partisan
- Use only established facts about current governors and parties; do NOT fabricate election results or alliances""",
    },
    {
        "key": "nigeria_foreign_policy",
        "title": "Nigeria's Foreign Policy Under Tinubu: Africa and Beyond",
        "tags": "nigeria foreign policy, tinubu, ecowas, africa, diplomacy, united nations, politics",
        "frequency_days": 21,
        "prompt": """You are a senior foreign affairs analyst at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, original HTML analysis article about Nigeria's foreign policy direction under President Tinubu.

TODAY'S DATE: {date_str}

CONTEXT: Nigeria is Africa's largest economy and most populous nation. Under Tinubu, Nigeria has maintained its ECOWAS leadership role, responded to the Niger coup (2023), pursued investment diplomacy, and maintained strong bilateral ties with the UK, US, and UAE. Nigeria is a member of the UN Security Council (non-permanent) and African Union. Your training data ends around August 2025 — focus on structural foreign policy dynamics; do NOT present bilateral negotiations or ECOWAS decisions as pending if they may have concluded.

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars>
ANALYSIS: <2–3 sentences of expert analysis — the most important foreign policy challenge Nigeria faces and whether the Tinubu government is handling it well>
---
<HTML article body>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li> — no <html>/<body>/<head> tags
- <h2>: headline about Nigeria's international standing and foreign policy
- Opening: Nigeria's role as Africa's diplomatic heavyweight and the expectations that come with it
- <h3>ECOWAS and West African Security</h3>: Nigeria's response to coups in Niger, Mali, Burkina Faso and the future of ECOWAS
- <h3>Investment and Economic Diplomacy</h3>: Tinubu's investor roadshows, diaspora engagement, key bilateral agreements
- <h3>Nigeria and Global Powers</h3>: relations with the US, UK, China, EU — opportunities and risks
- <h3>Unresolved Issues</h3>: Bakassi, Lake Chad basin, Boko Haram cross-border dimensions, undocumented diaspora
- Closing: what a bold Nigerian foreign policy agenda should prioritise for the remainder of the Tinubu term
- Length: 550–700 words | Tone: expert, measured, pro-Africa
- Use only established foreign policy facts; do NOT fabricate specific treaty terms or summit outcomes""",
    },
]


def build_static_politics_prompt(topic, date_str=""):
    return topic["prompt"].replace("{date_str}", date_str)


# ── Music chart fetcher ───────────────────────────────────────────────────────

def fetch_nigeria_music_charts(limit=10):
    """
    Fetch top songs from Apple Music Nigeria via the public Apple Marketing
    Tools RSS/JSON API.  Returns a list of dicts: {rank, song, artist}.
    Falls back to an empty list so the caller can degrade gracefully.
    """
    url = (
        f"https://rss.applemarketingtools.com/api/v2/ng/music/"
        f"most-played/{limit}/songs.json"
    )
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "PulseLineDaily/1.0"})
        r.raise_for_status()
        results = r.json().get("feed", {}).get("results", [])
        return [
            {
                "rank": idx + 1,
                "song": item.get("name", ""),
                "artist": item.get("artistName", ""),
            }
            for idx, item in enumerate(results)
        ]
    except Exception as exc:
        logger.warning("fetch_nigeria_music_charts failed: %s", exc)
        return []


# ── Entertainment analysis topics ─────────────────────────────────────────────

ENTERTAINMENT_ANALYSIS_TOPICS = [
    {
        "key": "afrobeats_weekly",
        "title": "Afrobeats Weekly: Nigeria's Hottest Songs and Artists",
        "tags": "afrobeats, nigerian music, burna boy, wizkid, davido, music chart, naija music, entertainment",
        "frequency_days": 7,
        "prompt": """You are a senior entertainment and music journalist at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, original HTML weekly Afrobeats roundup.

TODAY'S DATE: {date_str}

⚠️ REAL CHART DATA — Apple Music Nigeria Top Songs right now:
{chart_data}

CRITICAL: The numbered list above is LIVE DATA fetched TODAY. You MUST base the "This Week's Chart Leaders" section exclusively on these songs and artists. Do not substitute, invent, or add songs that are not on this list. Your knowledge cutoff means you do not know what is trending in {date_str} — trust the data above.

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars — name the #1 charting song or artist>
ANALYSIS: <2–3 sentences of editorial music analysis — the sound or trend these chart picks reveal>
---
<HTML article body>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li> — no <html>/<body>/<head>/<style> tags
- <h2>: headline referencing the #1 or most notable song from the chart data above
- Opening paragraph: set the scene using the chart data — what these songs collectively say about Nigerian music tastes right now
- <h3>This Week's Chart Leaders</h3>: write about the songs from the Apple Music Nigeria list above — for each, name the artist and song, note its position, and analyse why it is resonating; do not add songs not in the data
- <h3>Artists in Their Prime</h3>: based on the artists appearing in the chart data, analyse the ones with the most chart presence and their current momentum
- <h3>The Sound of the Moment</h3>: what musical styles and production trends do these charting songs reveal — Amapiano influence, Afrobeats, street-hop, Afro-soul, etc.
- <h3>Rising Stars to Watch</h3>: 2–3 newer or emerging Nigerian artists — only name artists you are confident about from your training knowledge
- <h3>Global Reach</h3>: Afrobeats' international footprint — crossover collaborations, chart placements, streaming milestones — use only established facts from your training
- Closing: a forward-looking sentence on where Nigerian music is headed
- Length: 700–900 words | Tone: vibrant, knowledgeable, celebratory
- SEO: "afrobeats 2026", "nigerian music chart", "hottest naija songs", "afrobeats weekly"
- Do NOT fabricate chart positions, streaming numbers, or songs not in the provided data""",
    },
    {
        "key": "nollywood_weekly",
        "title": "Nollywood Now: Movies and Series Every Nigerian Is Watching",
        "tags": "nollywood, nigerian movies, netflix nigeria, showmax, film, cinema, entertainment",
        "frequency_days": 7,
        "prompt": """You are a senior film and entertainment journalist at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, original HTML weekly Nollywood roundup covering what Nigerians are watching and talking about right now.

TODAY'S DATE: {date_str}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars — name the hottest Nollywood title right now>
ANALYSIS: <2–3 sentences of editorial analysis — the trend, theme, or creative wave defining Nollywood at this moment>
---
<HTML article body>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li> — no <html>/<body>/<head>/<style> tags
- <h2>: headline naming a specific film, series, or Nollywood moment dominating conversation
- Opening paragraph: set the scene — what Nollywood film or series is everyone talking about right now
- <h3>What to Watch This Week</h3>: 3–5 Nollywood films or series currently streaming or in cinemas — name the title, platform (Netflix, Showmax, Prime Video, cinema), lead actors, and a brief review or description; only name titles you are confident exist
- <h3>The Breakout Stars</h3>: 2–3 Nollywood actors or directors who are having a defining moment — their latest project, their trajectory, why they matter
- <h3>Nollywood Trends</h3>: recurring themes dominating Nigerian cinema right now — Yoruba romanticism, crime thrillers, political satire, diaspora stories, etc.
- <h3>International Recognition</h3>: Nollywood on the global stage — international festival selections, streaming deals, diaspora viewership — use only established facts
- Closing: what Nollywood title or event to look out for in the coming weeks
- Length: 700–900 words | Tone: engaging, culturally sharp, celebratory but honest
- SEO: "nollywood 2026", "nigerian movies to watch", "nollywood netflix", "naija movies"
- Do NOT fabricate box office numbers, specific streaming figures, or award wins you are not certain about""",
    },
    {
        "key": "nigerian_celebrity_culture",
        "title": "Nigerian Celebrity Culture: What Everyone Is Talking About",
        "tags": "nigerian celebrities, entertainment, bbnaija, social media, influencers, culture, entertainment",
        "frequency_days": 14,
        "prompt": """You are a senior entertainment journalist at PulseLineDaily, Nigeria's leading digital news outlet.

Write a complete, original HTML entertainment feature about the people, moments, and conversations dominating Nigerian celebrity culture.

TODAY'S DATE: {date_str}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars — name the celebrity moment or story capturing attention>
ANALYSIS: <2–3 sentences of editorial analysis — what this moment reveals about Nigerian celebrity culture and the broader entertainment industry>
---
<HTML article body>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li> — no <html>/<body>/<head>/<style> tags
- <h2>: headline capturing the biggest celebrity story or cultural moment right now
- Opening paragraph: introduce the dominant story or theme in Nigerian celebrity culture this week
- <h3>The Conversation Everyone Is Having</h3>: the top 2–3 stories or moments dominating Nigerian social media, Twitter/X, and entertainment blogs — be specific about who is involved and what happened; only cover stories you are confident about
- <h3>Ones to Watch</h3>: 3–4 celebrities — musicians, actors, comedians, influencers, skit makers — who are building major momentum right now and why
- <h3>Fashion and Style Moments</h3>: notable red carpet looks, fashion collaborations, or style moments that have sparked conversation — Nigerian designers and the global fashion stage
- <h3>What Social Media Is Saying</h3>: the tone of public conversation around these stories — praise, controversy, debate — without amplifying harmful content
- Closing: a sharp cultural observation about what these stories say about Nigeria in {date_str[:4]}
- Length: 650–850 words | Tone: culturally aware, stylish, engaging — the voice of someone who genuinely loves Nigerian entertainment
- SEO: "nigerian celebrities", "naija entertainment", "nigeria celebrity news"
- Do NOT publish unverified gossip, defamatory claims, or fabricated controversies""",
    },
]


def build_static_entertainment_prompt(topic, date_str="", chart_data=None):
    """Return the topic's prompt with date (and optionally chart data) injected."""
    prompt = topic["prompt"].replace("{date_str}", date_str)
    if chart_data is not None:
        prompt = prompt.replace("{chart_data}", chart_data)
    return prompt
