"""
Data journalism helpers: fetch open data + RSS stories, build Claude prompts.
No Django models imported here — keep this as pure Python / easily testable.
"""

import html as html_module
import logging
import re
import requests

logger = logging.getLogger(__name__)

# ── RSS Sources (Option C) ────────────────────────────────────────────────────
# Add / remove feeds freely.  category → matched against your Category table.
RSS_SOURCES = [
    {"url": "https://punchng.com/feed/",              "category": "News",        "label": "Punch NG"},
    {"url": "https://www.vanguardngr.com/feed/",       "category": "News",        "label": "Vanguard NG"},
    {"url": "https://businessday.ng/feed/",            "category": "Economy",     "label": "BusinessDay NG"},
    {"url": "https://techcabal.com/feed/",             "category": "Technology",  "label": "TechCabal"},
    {"url": "https://www.channelstv.com/feed/",        "category": "News",        "label": "Channels TV"},
    {"url": "https://guardian.ng/feed/",               "category": "News",        "label": "Guardian NG"},
    {"url": "https://www.premiumtimesng.com/feed/",    "category": "News",        "label": "Premium Times NG"},
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

INDICATOR: {info['name']}
BACKGROUND: {info['context']}

WORLD BANK DATA — Nigeria (NGA):
{history_lines}

Latest: {latest['value']}{info['unit']} ({latest['year']}). {trend_sentence}

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <one punchy sentence max 160 characters — the article card teaser, no "PulseLineDaily" branding>
ANALYSIS: <2–3 sentences of sharp expert takeaway — what this figure means at a macro level and what Nigerians should watch for next; no named quotes>
---
<full HTML article body starting with <h2>>

ARTICLE REQUIREMENTS:
- Pure HTML body content — use <h2>, <h3>, <p>, <strong>, <ul>, <li>
- Do NOT include <html>, <body>, <head>, or <style> tags
- Open with a strong <h2> headline that names the key figure and year
- Paragraph 1: state the key statistic and why it matters to Nigerians right now
- <h3>Trend Analysis</h3>: compare the past 3–5 years of data; note whether the situation is improving, worsening, or stable
- <h3>What This Means for Everyday Nigerians</h3>: 4–5 concrete implications for consumers, workers, businesses, or families — use bullet points
- <h3>Expert Perspective</h3>: 3–4 sentences of authoritative commentary — do NOT invent named quotes or named individuals
- <h3>Looking Ahead</h3>: a measured, forward-looking paragraph with what to watch in the coming months
- Attribute data to the World Bank
- Length: 600–800 words
- Tone: professional, balanced, accessible to a general Nigerian audience
- Naturally include SEO keywords: "Nigeria {info['name'].lower()}", "{info['category'].lower()} Nigeria {latest['year']}"
- Do NOT invent statistics beyond the data provided above"""


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


# ── Option C: RSS fetch + AI rewrite ─────────────────────────────────────────

def _strip_html(raw):
    """Remove HTML tags and decode entities from an RSS excerpt."""
    text = html_module.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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
                # Use summary, then description, then content — whichever exists
                raw_excerpt = (
                    entry.get("summary")
                    or entry.get("description")
                    or (entry.get("content") or [{}])[0].get("value", "")
                )
                excerpt = _strip_html(raw_excerpt)[:600]
                link = entry.get("link") or ""
                if not link:
                    continue
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
    Build a Claude prompt that turns an RSS story topic into a fully
    original PulseLineDaily article. Only the headline and a short excerpt
    are passed — Claude writes every word from scratch.
    """
    return f"""You are a senior journalist at PulseLineDaily, a leading Nigerian digital news outlet.

A story has just broken. Your job is to write a completely original news article about this topic for PulseLineDaily's audience.

STORY TOPIC:
Headline: {story['title']}
Brief background: {story['excerpt']}
Original source outlet: {story['source_label']} (do NOT copy their text)

YOUR TASK:
Write a fully original 500–650 word HTML news article about this topic. Every sentence must be your own writing — do not reproduce or closely paraphrase the source text above. Use the topic and facts only as your starting point.

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <one punchy sentence (max 160 characters) that teases the story for the article card — no "PulseLineDaily" branding, no "analysis of", just the story hook>
ANALYSIS: <2–3 sentences of sharp editorial analysis — the deeper significance of this story for Nigeria, what it reveals about a broader trend, and what to watch next>
---
<full HTML article body starting with <h2>>

ARTICLE REQUIREMENTS:
- Pure HTML — use <h2>, <h3>, <p>, <strong>, <ul>, <li>
- Do NOT include <html>, <body>, <head>, or <style> tags
- Open with a strong, original <h2> headline (you may rewrite the source headline)
- Paragraph 1: a gripping lead that states the key development clearly
- <h3>Background and Context</h3>: relevant background a Nigerian reader needs to understand the issue
- <h3>Why This Matters to Nigerians</h3>: 3–4 concrete implications for everyday Nigerians — economic, social, or political
- <h3>What Happens Next</h3>: a measured, factual analysis of likely next steps or developments
- Closing paragraph: a concise, forward-looking sentence
- Tone: authoritative, clear, engaging — quality Nigerian news outlet voice
- Do NOT invent specific statistics, named quotes, or named individuals you are not certain of
- Include natural SEO keywords relevant to the Nigerian context"""


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


def detect_story_category(title, excerpt, default_category):
    """
    Return 'Sports' if the story title or excerpt contains sports keywords,
    otherwise return the default_category from the RSS source config.
    """
    text = (title + " " + excerpt).lower()
    if any(kw in text for kw in SPORTS_KEYWORDS):
        return "Sports"
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
        for group in data.get("children", []):
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
- <h3>World Cup 2026 Implications</h3>: how this form affects Nigeria's CAF qualifying position and World Cup 2026 prospects
- <h3>Fan Verdict</h3>: a balanced take on what Nigerian fans should feel — encouragement or concern?
- Closing: forward-looking sentence about upcoming fixtures or goals
- Length: 500–650 words | Tone: passionate, expert, balanced
- Do NOT invent players, managers, or scores not shown in the data above"""


# Static sports analysis topics (no live data needed — AI uses training knowledge)
SPORTS_ANALYSIS_TOPICS = [
    {
        "key": "world_cup_2026_africa",
        "title": "World Cup 2026: Which African Nations Will Make It?",
        "tags": "world cup 2026, africa, caf, nigeria, super eagles, football",
        "frequency_days": 14,
        "prompt": """You are a senior sports journalist at PulseLineDaily.

Write a complete, original HTML sports analysis article about Africa's prospects at the 2026 FIFA World Cup.

CONTEXT: The 2026 World Cup will be hosted by USA, Canada, and Mexico. Africa (CAF) has been allocated 9 spots — the most ever. CAF Round 3 qualifying groups are currently deciding which nations will make it.

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars>
ANALYSIS: <2–3 sentences of expert analysis — Nigeria's realistic chances and the key factor that will decide Africa's best performers in 2026>
---
<HTML article body>

ARTICLE REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li>
- <h2>: strong headline about Africa and World Cup 2026
- Opening: why 2026 is a historic opportunity for African football
- <h3>Nigeria's Chances</h3>: Super Eagles CAF qualifying position and realistic prospects
- <h3>Africa's Strongest Teams</h3>: top 4–5 African contenders (Morocco, Senegal, Egypt, Ivory Coast, South Africa, Nigeria, etc.) — use only nations whose form you know well
- <h3>The 9-Slot Advantage</h3>: what the increased allocation means for smaller African nations
- <h3>Prediction</h3>: your informed prediction of which 9 nations will represent Africa
- Closing: what success would mean for African football's global standing
- Length: 550–700 words | Tone: expert, analytical, optimistic but balanced
- Base your analysis on established football knowledge; do NOT fabricate specific qualifying scores""",
    },
    {
        "key": "nigeria_football_history",
        "title": "Nigeria at the World Cup: Every Appearance Reviewed",
        "tags": "super eagles, nigeria, world cup history, football, nwankwo kanu, jay jay okocha",
        "frequency_days": 30,
        "prompt": """You are a senior sports journalist at PulseLineDaily.

Write a complete, original HTML sports feature about Nigeria's history at the FIFA World Cup.

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars>
ANALYSIS: <2–3 sentences reflecting on Nigeria's World Cup legacy — what the history reveals about the team's potential and what separates the great campaigns from the disappointments>
---
<HTML article body>

REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li>
- <h2>: compelling headline about Nigeria's World Cup story
- Opening: Nigeria's overall World Cup record and significance to African football
- For each tournament Nigeria appeared in (1994, 1998, 2002, 2010, 2014, 2018): a short paragraph covering squad highlights, results, and what went wrong or right
- <h3>Greatest World Cup Moments</h3>: top 3 moments in Nigerian World Cup history
- <h3>The Players Who Defined the Stage</h3>: Jay-Jay Okocha, Nwankwo Kanu, Rashidi Yekini, Vincent Enyeama, John Obi Mikel — brief tributes
- <h3>Looking Ahead to 2026</h3>: one paragraph on what Nigeria needs to do to restore World Cup glory
- Length: 600–750 words | Tone: nostalgic, celebratory, expert
- Use only established historical facts you are confident about""",
    },
    {
        "key": "african_football_weekly",
        "title": "African Football Roundup: This Week's Key Stories",
        "tags": "african football, caf, super eagles, nigeria, afcon, champions league africa",
        "frequency_days": 7,
        "prompt": """You are a senior sports journalist at PulseLineDaily.

Write a complete, original HTML weekly sports roundup covering African football.

OUTPUT FORMAT — three parts, exactly as shown:
SUMMARY: <punchy one-liner teaser, max 160 chars>
ANALYSIS: <2–3 sentences of editorial analysis — the single biggest story in African football right now and what it means for the continent's standing in the global game>
---
<HTML article body>

REQUIREMENTS:
- Pure HTML — <h2>, <h3>, <p>, <strong>, <ul>, <li>
- <h2>: African Football Weekly Roundup headline
- Cover 4–5 key themes or storylines currently circulating in African football (use general knowledge — do not fabricate specific recent match scores)
- Themes to consider: CAF Champions League, World Cup 2026 qualifiers, NPFL Nigeria league, key transfers, managerial changes, rising African stars in Europe
- <h3>Super Eagles Watch</h3>: Nigeria-specific section — squad updates, upcoming matches, qualification status
- <h3>Ones to Watch</h3>: 2–3 African players making headlines in European leagues
- Closing: what to look out for in the coming week of African football
- Length: 500–600 words | Tone: lively, fan-first, expert
- Clearly distinguish between established fact and general assessment""",
    },
]


def build_static_sports_prompt(topic):
    return topic["prompt"]
