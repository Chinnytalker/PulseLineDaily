import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def send_weekly_newsletter():
    from django.conf import settings
    from django.core.mail import EmailMultiAlternatives
    from django.core import signing
    from django.template.loader import render_to_string
    from django.utils import timezone
    from datetime import timedelta
    from .models import NewsletterSubscriber, Post

    subscribers = list(
        NewsletterSubscriber.objects.filter(is_active=True).values_list('email', flat=True)
    )
    if not subscribers:
        return "No confirmed subscribers."

    week_ago = timezone.now() - timedelta(days=7)
    top_posts = list(
        Post.published.filter(date_created__gte=week_ago).order_by('-views')[:5]
    )
    if not top_posts:
        return "No new posts this week."

    week_label = timezone.now().strftime("%B %d, %Y")
    base_url = "https://www.pulselinedaily.com"
    posts_ctx = [
        {
            "title": p.title,
            "summary": p.summary or p.body[:140],
            "url": f"{base_url}{p.get_absolute_url()}",
        }
        for p in top_posts
    ]

    sent = 0
    for email in subscribers:
        try:
            unsub_token = signing.dumps(email, salt='newsletter-unsub')
            unsubscribe_url = f"{base_url}/newsletter/unsubscribe/{unsub_token}/"

            html_body = render_to_string('blog/email/newsletter_weekly.html', {
                'posts': posts_ctx,
                'week_label': week_label,
                'unsubscribe_url': unsubscribe_url,
            })

            plain_lines = [f"This week's top stories on PulseLineDaily ({week_label}):\n"]
            for i, p in enumerate(posts_ctx, 1):
                plain_lines.append(f"{i}. {p['title']}")
                plain_lines.append(f"   {p['summary']}")
                plain_lines.append(f"   Read more: {p['url']}\n")
            plain_lines += [
                "─────────────────────────────────",
                "You're receiving this because you confirmed your subscription.",
                f"Unsubscribe: {unsubscribe_url}",
            ]

            msg = EmailMultiAlternatives(
                subject="PulseLineDaily — Weekly Digest",
                body="\n".join(plain_lines),
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[email],
            )
            msg.attach_alternative(html_body, "text/html")
            msg.send()
            sent += 1
        except Exception as exc:
            logger.warning("Newsletter failed for %s: %s", email, exc)

    return f"Newsletter sent to {sent}/{len(subscribers)} confirmed subscribers."


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def generate_data_journalism_articles(self):
    """
    Fetch open data from the World Bank and Open Exchange Rates, then use the
    Groq API (free) to write original data-journalism articles as unpublished drafts.
    Runs daily via Celery Beat; humans review in admin before publishing.
    """
    from django.conf import settings
    from django.utils import timezone
    from datetime import timedelta

    api_key = getattr(settings, "GROQ_API_KEY", "")
    if not api_key:
        logger.warning("GROQ_API_KEY not set — skipping data journalism task")
        return "GROQ_API_KEY not configured"

    try:
        from groq import Groq
    except ImportError:
        logger.error("groq package not installed — run: pip install groq")
        return "groq package missing"

    from .models import Post, Category
    from .data_journalism import (
        INDICATORS,
        fetch_worldbank_indicator,
        fetch_usd_ngn_rate,
        build_indicator_prompt,
        build_exchange_rate_prompt,
    )

    client = Groq(api_key=api_key)
    now = timezone.now()
    month_year = now.strftime("%B %Y")   # e.g. "June 2025"
    date_str = now.strftime("%d %B %Y")  # e.g. "24 June 2025"
    created = 0

    from .models import Author
    default_author = Author.objects.filter(slug="clinton-nwachukwu").first()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def call_llm(prompt):
        try:
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.error("Groq API error: %s", exc)
            return None

    def parse_response(raw):
        """Extract SUMMARY, ANALYSIS, and HTML body from LLM response."""
        summary, analysis, html = "", "", raw
        if "---" in raw:
            head, body = raw.split("---", 1)
            html = body.strip()
            current_key = None
            buf = []
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
        return summary, analysis, html

    def category_for(hint):
        """Return the first Category whose name contains hint (case-insensitive)."""
        return Category.objects.filter(name__icontains=hint).first()

    def recently_generated(tag_snippet, days):
        """True if we already made an auto article with this tag substring recently."""
        cutoff = now - timedelta(days=days)
        return Post.objects.filter(
            updated_by="data-auto",
            tags__icontains=tag_snippet,
            date_created__gte=cutoff,
        ).exists()

    def make_post(title, body, summary, analysis, tags, source_domain, category_hint):
        cat = (
            category_for(category_hint)
            or category_for("economy")
            or category_for("news")
            or Category.objects.first()
        )
        post = Post.objects.create(
            title=title,
            body=body,
            summary=summary,
            analysis=analysis or None,
            tags=tags,
            source="api",
            source_domain=source_domain,
            is_published=False,
            updated_by="data-auto",
            author=default_author,
        )
        if cat:
            post.categories.add(cat)
        logger.info("Created data-journalism draft: %s", title)
        return post

    # ── 1. World Bank economic/social indicator (rotates daily) ──────────────

    indicator_keys = list(INDICATORS.keys())
    # Deterministic daily rotation: day-of-year modulo number of indicators
    day_index = now.timetuple().tm_yday % len(indicator_keys)
    ind_key = indicator_keys[day_index]
    info = INDICATORS[ind_key]

    if not recently_generated(ind_key, days=6):
        data_points = fetch_worldbank_indicator(info["code"])
        if data_points and len(data_points) >= 2:
            prompt = build_indicator_prompt(ind_key, info, data_points)
            raw = call_llm(prompt)
            if raw:
                summary, analysis, html = parse_response(raw)
                latest_year = data_points[-1]["year"]
                if not summary:
                    summary = (
                        f"An in-depth analysis of Nigeria's {info['name'].lower()} "
                        f"based on World Bank data for {latest_year}."
                    )
                make_post(
                    title=f"Nigeria {info['name']}: {month_year} Analysis",
                    body=html,
                    summary=summary,
                    analysis=analysis,
                    tags=(
                        f"nigeria, {ind_key}, {info['category'].lower()}, "
                        f"world bank, data journalism, {latest_year}"
                    ),
                    source_domain="data.worldbank.org",
                    category_hint=info["category"],
                )
                created += 1
        else:
            logger.info("World Bank returned no data for %s — skipping", ind_key)

    # ── 2. Daily exchange rate article ────────────────────────────────────────

    if not recently_generated("exchange rate", days=1):
        rate_data = fetch_usd_ngn_rate()
        if rate_data:
            prompt = build_exchange_rate_prompt(rate_data)
            raw = call_llm(prompt)
            if raw:
                summary, analysis, html = parse_response(raw)
                rate = rate_data["rate"]
                if not summary:
                    summary = (
                        f"The USD to Naira exchange rate today is ₦{rate:,.0f} per dollar. "
                        f"Here is what this means for Nigerian consumers and businesses."
                    )
                make_post(
                    title=f"Naira Exchange Rate Today — {date_str}: 1 USD = ₦{rate:,.0f}",
                    body=html,
                    summary=summary,
                    analysis=analysis,
                    tags=(
                        "nigeria, exchange rate, naira, dollar, forex, economy, cbn, "
                        f"usd ngn, {now.strftime('%Y')}"
                    ),
                    source_domain="open.er-api.com",
                    category_hint="economy",
                )
                created += 1

    return f"Data journalism task complete: {created} draft(s) created"


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def generate_rss_articles(self):
    """
    Option C: Fetch story topics from Nigerian RSS feeds and rewrite each as a
    fully original article using Groq (free). Saves up to 3 unpublished drafts per run.
    Deduplication is done by checking the original story URL (stored in post.link).
    """
    from django.conf import settings
    from django.utils import timezone
    from datetime import timedelta

    api_key = getattr(settings, "GROQ_API_KEY", "")
    if not api_key:
        logger.warning("GROQ_API_KEY not set — skipping RSS rewrite task")
        return "GROQ_API_KEY not configured"

    try:
        from groq import Groq
    except ImportError:
        logger.error("groq package not installed — run: pip install groq")
        return "groq package missing"

    from .models import Post, Category, Author
    from .data_journalism import fetch_rss_stories, fetch_article_body, build_rewrite_prompt, detect_story_category

    client = Groq(api_key=api_key)
    now = timezone.now()
    created = 0
    MAX_PER_RUN = 3  # cap articles created per task run

    default_author = Author.objects.filter(slug="clinton-nwachukwu").first()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def call_llm(prompt):
        try:
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.error("Groq API error: %s", exc)
            return None

    def parse_response(raw):
        summary, analysis, html = "", "", raw
        if "---" in raw:
            head, body = raw.split("---", 1)
            html = body.strip()
            current_key = None
            buf = []
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
        return summary, analysis, html

    def category_for(hint):
        return Category.objects.filter(name__icontains=hint).first()

    def best_category(hint):
        return (
            category_for(hint)
            or category_for("news")
            or Category.objects.first()
        )

    def already_exists(story_link):
        """Skip if we already wrote an article for this exact story URL."""
        return Post.objects.filter(link=story_link).exists()

    _STOPWORDS = {
        "a", "an", "the", "is", "are", "was", "were", "of", "in", "on", "at",
        "to", "for", "with", "and", "or", "but", "as", "by", "from", "that",
        "this", "it", "its", "be", "has", "have", "had", "will", "not", "no",
        "up", "out", "how", "why", "what", "who", "when", "where", "says",
        "over", "than", "more", "amid", "after", "about", "been", "into",
        "some", "also", "just", "its", "than", "other", "their", "there",
        "which", "they", "would", "could", "should",
    }

    def _keywords(text):
        import re as _re
        return {
            w for w in _re.findall(r'\b[a-z]{4,}\b', text.lower())
            if w not in _STOPWORDS
        }

    def similar_topic_covered(title):
        """True if a post created in the last 3 days shares 3+ keywords with this title."""
        kw = _keywords(title)
        if len(kw) < 3:
            return False
        cutoff = now - timedelta(days=3)
        recent_titles = Post.objects.filter(
            date_created__gte=cutoff,
        ).values_list("title", flat=True)[:300]
        for existing in recent_titles:
            if len(kw & _keywords(existing)) >= 3:
                return True
        return False

    # ── Fetch & rewrite ───────────────────────────────────────────────────────

    stories = fetch_rss_stories(max_per_source=2)

    for story in stories:
        if created >= MAX_PER_RUN:
            break

        if already_exists(story["link"]):
            logger.debug("Already covered (URL): %s", story["link"])
            continue

        if similar_topic_covered(story["title"]):
            logger.debug("Similar topic already covered: %s", story["title"])
            continue

        # Only fetch the full article body after both dedup checks pass
        story["body_text"] = fetch_article_body(story["link"], max_chars=3000)

        prompt = build_rewrite_prompt(story)
        raw = call_llm(prompt)
        if not raw:
            continue

        summary, analysis, html = parse_response(raw)
        if not summary:
            summary = story["title"][:160]

        title = story["title"][:195]
        resolved_cat = detect_story_category(story["title"], story["excerpt"], story["category"])
        cat = best_category(resolved_cat)

        try:
            post = Post.objects.create(
                title=title,
                body=html,
                summary=summary,
                analysis=analysis or None,
                tags=f"nigeria, {story['category'].lower()}, breaking news, analysis",
                link=story["link"][:500],
                source="api",
                source_domain=story["source_label"],
                is_published=False,
                updated_by="rss-auto",
                author=default_author,
            )
            if cat:
                post.categories.add(cat)
            logger.info("Created RSS rewrite draft: %s", title)
            created += 1
        except Exception as exc:
            logger.error("Failed to save RSS draft [%s]: %s", title[:80], exc)

    return f"RSS rewrite task complete: {created} draft(s) created"


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def generate_sports_articles(self):
    """
    Sports content pipeline:
    1. EPL standings analysis (live data via ESPN public API)
    2. Nigeria Super Eagles recent results (live data via TheSportsDB)
    3. Rotating static sports analysis topics (World Cup 2026, history, roundup)
    All saved as unpublished drafts credited to Clinton Nwachukwu.
    """
    from django.conf import settings
    from django.utils import timezone
    from datetime import timedelta

    api_key = getattr(settings, "GROQ_API_KEY", "")
    if not api_key:
        logger.warning("GROQ_API_KEY not set — skipping sports articles task")
        return "GROQ_API_KEY not configured"

    try:
        from groq import Groq
    except ImportError:
        return "groq package missing"

    from .models import Post, Category, Author
    from .data_journalism import (
        fetch_epl_standings,
        fetch_nigeria_results,
        fetch_world_cup_2026_data,
        fetch_wc2026_fixtures,
        fetch_wc2026_top_scorers,
        build_epl_standings_prompt,
        build_nigeria_results_prompt,
        build_world_cup_2026_prompt,
        build_wc2026_match_preview_prompt,
        build_wc2026_golden_boot_prompt,
        SPORTS_ANALYSIS_TOPICS,
        build_static_sports_prompt,
    )

    client = Groq(api_key=api_key)
    now = timezone.now()
    created = 0

    default_author = Author.objects.filter(slug="clinton-nwachukwu").first()
    sports_cat = (
        Category.objects.filter(name__icontains="sport").first()
        or Category.objects.first()
    )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def call_llm(prompt):
        try:
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.error("Groq API error: %s", exc)
            return None

    def parse_response(raw):
        summary, analysis, html = "", "", raw
        if "---" in raw:
            head, body = raw.split("---", 1)
            html = body.strip()
            current_key = None
            buf = []
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
        return summary, analysis, html

    def recently_generated(tag_snippet, days):
        cutoff = now - timedelta(days=days)
        return Post.objects.filter(
            updated_by="sports-auto",
            tags__icontains=tag_snippet,
            date_created__gte=cutoff,
        ).exists()

    def save_draft(title, body, summary, analysis, tags):
        post = Post.objects.create(
            title=title,
            body=body,
            summary=summary or title,
            analysis=analysis or None,
            tags=tags,
            source="api",
            is_published=False,
            updated_by="sports-auto",
            author=default_author,
        )
        if sports_cat:
            post.categories.add(sports_cat)
        logger.info("Created sports draft: %s", title)
        return post

    date_str = now.strftime("%d %B %Y")

    # ── 1. WC2026 live standings & results (daily) ───────────────────────────
    if not recently_generated("world cup 2026", days=1):
        wc_data = fetch_world_cup_2026_data()
        if wc_data:
            raw = call_llm(build_world_cup_2026_prompt(wc_data, date_str))
            if raw:
                summary, analysis, html = parse_response(raw)
                save_draft(
                    title=f"World Cup 2026 Live Update: Standings & Results — {date_str}",
                    body=html,
                    summary=summary,
                    analysis=analysis,
                    tags="world cup 2026, football, group stage, round of 32, sports, standings",
                )
                created += 1
        else:
            logger.info("WC2026 data unavailable from ESPN — skipping live WC article")

    # ── 2. WC2026 match preview (daily — today's fixtures) ───────────────────
    if not recently_generated("wc2026 preview", days=1):
        fixtures = fetch_wc2026_fixtures()
        if fixtures:
            raw = call_llm(build_wc2026_match_preview_prompt(fixtures, date_str))
            if raw:
                summary, analysis, html = parse_response(raw)
                save_draft(
                    title=f"World Cup 2026 Match Preview — {date_str}",
                    body=html,
                    summary=summary,
                    analysis=analysis,
                    tags="world cup 2026, match preview, football, fixtures, wc2026 preview, sports",
                )
                created += 1
        else:
            logger.info("WC2026 fixtures unavailable — skipping match preview")

    # ── 3. WC2026 Golden Boot race (every 2 days) ────────────────────────────
    if not recently_generated("golden boot", days=2):
        scorers = fetch_wc2026_top_scorers()
        if scorers:
            raw = call_llm(build_wc2026_golden_boot_prompt(scorers, date_str))
            if raw:
                summary, analysis, html = parse_response(raw)
                save_draft(
                    title=f"World Cup 2026 Golden Boot Race: Top Scorers — {date_str}",
                    body=html,
                    summary=summary,
                    analysis=analysis,
                    tags="world cup 2026, golden boot, top scorers, football, sports",
                )
                created += 1
        else:
            logger.info("WC2026 top scorers unavailable — skipping golden boot article")

    # ── 4. EPL standings (once per 3 days — EPL season Aug–May only) ─────────
    if not recently_generated("premier league", days=3):
        table = fetch_epl_standings()
        if table:
            raw = call_llm(build_epl_standings_prompt(table))
            if raw:
                summary, analysis, html = parse_response(raw)
                save_draft(
                    title=f"Premier League Table: Where Every Club Stands — {now.strftime('%B %Y')}",
                    body=html,
                    summary=summary,
                    analysis=analysis,
                    tags="premier league, epl, football, sports, nigeria, standings",
                )
                created += 1

    # ── 5. Nigeria Super Eagles results (once per 4 days) ────────────────────
    if not recently_generated("super eagles", days=4):
        data = fetch_nigeria_results()
        if data and data.get("results"):
            raw = call_llm(build_nigeria_results_prompt(data))
            if raw:
                summary, analysis, html = parse_response(raw)
                save_draft(
                    title=f"Super Eagles: Recent Results Analysed — {date_str}",
                    body=html,
                    summary=summary,
                    analysis=analysis,
                    tags="super eagles, nigeria, football, world cup 2026, sports",
                )
                created += 1

    # ── 6. Static sports analysis topics (rotate by day of month) ────────────
    topic_index = now.day % len(SPORTS_ANALYSIS_TOPICS)
    topic = SPORTS_ANALYSIS_TOPICS[topic_index]

    if not recently_generated(topic["key"], days=topic["frequency_days"]):
        raw = call_llm(build_static_sports_prompt(topic, date_str=date_str))
        if raw:
            summary, analysis, html = parse_response(raw)
            save_draft(
                title=f"{topic['title']} — {now.strftime('%B %Y')}",
                body=html,
                summary=summary,
                analysis=analysis,
                tags=f"{topic['tags']}, {topic['key']}",
            )
            created += 1

    return f"Sports articles task complete: {created} draft(s) created"


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def generate_market_articles(self):
    """
    Market & security data journalism pipeline:
    1. Brent crude oil price (daily) — Yahoo Finance
    2. NGX All-Share Index (weekdays only) — Yahoo Finance
    3. Agricultural commodity prices (every 3 days) — Yahoo Finance + World Bank
    4. Nigeria security briefing (every 3 days, optional) — ACLED API
    All saved as unpublished drafts for human review.
    """
    from django.conf import settings
    from django.utils import timezone
    from datetime import timedelta

    api_key = getattr(settings, "GROQ_API_KEY", "")
    if not api_key:
        logger.warning("GROQ_API_KEY not set — skipping market articles task")
        return "GROQ_API_KEY not configured"

    try:
        from groq import Groq
    except ImportError:
        return "groq package missing"

    from .models import Post, Category, Author
    from .data_journalism import (
        fetch_brent_crude,
        fetch_ngx_index,
        fetch_commodity_prices,
        fetch_acled_nigeria,
        fetch_crypto_prices,
        fetch_parallel_market_rate,
        build_brent_prompt,
        build_ngx_prompt,
        build_commodity_prompt,
        build_acled_prompt,
        build_crypto_prompt,
        build_parallel_rate_prompt,
    )

    client = Groq(api_key=api_key)
    now = timezone.now()
    date_str = now.strftime("%d %B %Y")
    created = 0

    default_author = Author.objects.filter(slug="clinton-nwachukwu").first()

    def call_llm(prompt):
        try:
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.error("Groq API error: %s", exc)
            return None

    def parse_response(raw):
        summary, analysis, html = "", "", raw
        if "---" in raw:
            head, body = raw.split("---", 1)
            html = body.strip()
            current_key = None
            buf = []
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
        return summary, analysis, html

    def recently_generated(tag_snippet, days):
        cutoff = now - timedelta(days=days)
        return Post.objects.filter(
            updated_by="market-auto",
            tags__icontains=tag_snippet,
            date_created__gte=cutoff,
        ).exists()

    def category_for(hint):
        return Category.objects.filter(name__icontains=hint).first()

    def save_draft(title, body, summary, analysis, tags, category_hint):
        cat = (
            category_for(category_hint)
            or category_for("economy")
            or category_for("news")
            or Category.objects.first()
        )
        post = Post.objects.create(
            title=title,
            body=body,
            summary=summary or title[:160],
            analysis=analysis or None,
            tags=tags,
            source="api",
            is_published=False,
            updated_by="market-auto",
            author=default_author,
        )
        if cat:
            post.categories.add(cat)
        logger.info("Created market draft: %s", title)
        return post

    # ── 1. Brent crude price (daily) ──────────────────────────────────────────
    if not recently_generated("brent crude", days=1):
        data = fetch_brent_crude()
        if data:
            raw = call_llm(build_brent_prompt(data))
            if raw:
                summary, analysis, html = parse_response(raw)
                save_draft(
                    title=f"Brent Crude Today: ${data['brent_price']:.2f}/barrel — {date_str}",
                    body=html,
                    summary=summary,
                    analysis=analysis,
                    tags=(
                        f"brent crude, oil price, nigeria, opec, energy, economy, "
                        f"naira, {now.strftime('%Y')}"
                    ),
                    category_hint="economy",
                )
                created += 1
        else:
            logger.info("Brent crude fetch failed — skipping oil article")

    # ── 2. NGX All-Share Index (weekdays only, daily) ─────────────────────────
    if now.weekday() < 5 and not recently_generated("ngx", days=1):
        data = fetch_ngx_index()
        if data:
            raw = call_llm(build_ngx_prompt(data, date_str))
            if raw:
                summary, analysis, html = parse_response(raw)
                direction = "Gains" if data["change_pct"] >= 0 else "Decline"
                save_draft(
                    title=f"NGX All-Share Index {direction}: Market Wrap — {date_str}",
                    body=html,
                    summary=summary,
                    analysis=analysis,
                    tags=(
                        f"ngx, nigerian exchange, stock market, equities, "
                        f"investing, economy, {now.strftime('%Y')}"
                    ),
                    category_hint="economy",
                )
                created += 1
        else:
            logger.info("NGX index fetch failed — skipping stock market article")

    # ── 3. Agricultural commodity prices (every 3 days) ──────────────────────
    if not recently_generated("cocoa", days=3):
        data = fetch_commodity_prices()
        if data:
            raw = call_llm(build_commodity_prompt(data))
            if raw:
                summary, analysis, html = parse_response(raw)
                save_draft(
                    title=f"Nigeria Agricultural Commodity Prices — {now.strftime('%B %Y')}",
                    body=html,
                    summary=summary,
                    analysis=analysis,
                    tags=(
                        f"cocoa, palm oil, groundnuts, agriculture, commodities, "
                        f"nigeria, export, farmers, {now.strftime('%Y')}"
                    ),
                    category_hint="economy",
                )
                created += 1
        else:
            logger.info("Commodity price fetch returned no data — skipping agriculture article")

    # ── 4. ACLED security briefing (every 3 days, optional) ──────────────────
    acled_key = getattr(settings, "ACLED_API_KEY", "")
    acled_email = getattr(settings, "ACLED_EMAIL", "")
    if acled_key and acled_email and not recently_generated("security", days=3):
        data = fetch_acled_nigeria(acled_key, acled_email, days=30)
        if data:
            raw = call_llm(build_acled_prompt(data))
            if raw:
                summary, analysis, html = parse_response(raw)
                save_draft(
                    title=f"Nigeria Security Briefing: Conflict & Incident Report — {now.strftime('%B %Y')}",
                    body=html,
                    summary=summary,
                    analysis=analysis,
                    tags=(
                        f"nigeria security, conflict, insecurity, banditry, "
                        f"terrorism, acled, {now.strftime('%Y')}, security"
                    ),
                    category_hint="news",
                )
                created += 1

    # ── 5. Naira parallel market rate vs CBN (daily) ─────────────────────────
    if not recently_generated("parallel market", days=1):
        fx_data = fetch_parallel_market_rate()
        if fx_data:
            raw = call_llm(build_parallel_rate_prompt(fx_data, date_str))
            if raw:
                summary, analysis, html = parse_response(raw)
                parallel = fx_data.get("parallel") or fx_data.get("official", "")
                title_rate = f"₦{parallel:,.0f}" if parallel else "Today"
                save_draft(
                    title=f"Black Market Dollar Rate Today: {title_rate} — {date_str}",
                    body=html,
                    summary=summary,
                    analysis=analysis,
                    tags=(
                        "naira, dollar, black market, parallel market, aboki, "
                        "cbn exchange rate, forex, nigeria, economy"
                    ),
                    category_hint="economy",
                )
                created += 1
        else:
            logger.info("Parallel market rate fetch failed — skipping")

    # ── 6. Crypto prices — BTC & ETH (daily) ─────────────────────────────────
    if not recently_generated("bitcoin", days=1):
        crypto_data = fetch_crypto_prices()
        if crypto_data:
            raw = call_llm(build_crypto_prompt(crypto_data, date_str))
            if raw:
                summary, analysis, html = parse_response(raw)
                btc_price = (crypto_data.get("btc") or {}).get("price", "")
                title_price = f"${btc_price:,.0f}" if btc_price else "Today"
                save_draft(
                    title=f"Bitcoin & Crypto Prices Today: BTC at {title_price} — {date_str}",
                    body=html,
                    summary=summary,
                    analysis=analysis,
                    tags=(
                        f"bitcoin, ethereum, crypto, btc, eth, cryptocurrency, "
                        f"nigeria, naira, digital assets, {now.strftime('%Y')}"
                    ),
                    category_hint="technology",
                )
                created += 1
        else:
            logger.info("Crypto price fetch failed — skipping crypto article")

    return f"Market articles task complete: {created} draft(s) created"


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def generate_politics_articles(self):
    """
    Nigeria politics analysis pipeline:
    Rotating static analysis topics — elections, governance scorecard,
    National Assembly watch, state politics, foreign policy.
    Saved as unpublished drafts under the Politics category.
    """
    from django.conf import settings
    from django.utils import timezone
    from datetime import timedelta

    api_key = getattr(settings, "GROQ_API_KEY", "")
    if not api_key:
        logger.warning("GROQ_API_KEY not set — skipping politics articles task")
        return "GROQ_API_KEY not configured"

    try:
        from groq import Groq
    except ImportError:
        return "groq package missing"

    from .models import Post, Category, Author
    from .data_journalism import POLITICS_ANALYSIS_TOPICS, build_static_politics_prompt

    client = Groq(api_key=api_key)
    now = timezone.now()
    created = 0

    default_author = Author.objects.filter(slug="clinton-nwachukwu").first()
    politics_cat = (
        Category.objects.filter(name__icontains="polit").first()
        or Category.objects.filter(name__icontains="news").first()
        or Category.objects.first()
    )

    def call_llm(prompt):
        try:
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.error("Groq API error: %s", exc)
            return None

    def parse_response(raw):
        summary, analysis, html = "", "", raw
        if "---" in raw:
            head, body = raw.split("---", 1)
            html = body.strip()
            current_key = None
            buf = []
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
        return summary, analysis, html

    def recently_generated(tag_snippet, days):
        cutoff = now - timedelta(days=days)
        return Post.objects.filter(
            updated_by="politics-auto",
            tags__icontains=tag_snippet,
            date_created__gte=cutoff,
        ).exists()

    def save_draft(title, body, summary, analysis, tags):
        post = Post.objects.create(
            title=title,
            body=body,
            summary=summary or title,
            analysis=analysis or None,
            tags=tags,
            source="api",
            is_published=False,
            updated_by="politics-auto",
            author=default_author,
        )
        if politics_cat:
            post.categories.add(politics_cat)
        logger.info("Created politics draft: %s", title)
        return post

    # Rotate through topics by day-of-month
    topic_index = now.day % len(POLITICS_ANALYSIS_TOPICS)
    topic = POLITICS_ANALYSIS_TOPICS[topic_index]

    if not recently_generated(topic["key"], days=topic["frequency_days"]):
        raw = call_llm(build_static_politics_prompt(topic))
        if raw:
            summary, analysis, html = parse_response(raw)
            save_draft(
                title=f"{topic['title']} — {now.strftime('%B %Y')}",
                body=html,
                summary=summary,
                analysis=analysis,
                tags=f"{topic['tags']}, {topic['key']}",
            )
            created += 1

    return f"Politics articles task complete: {created} draft(s) created"


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def generate_entertainment_articles(self):
    """
    Entertainment content pipeline:
    Rotating static topics — Afrobeats weekly, Nollywood now, Nigerian celebrity culture.
    Saved as unpublished drafts under the Entertainment category.
    """
    from django.conf import settings
    from django.utils import timezone
    from datetime import timedelta

    api_key = getattr(settings, "GROQ_API_KEY", "")
    if not api_key:
        logger.warning("GROQ_API_KEY not set — skipping entertainment articles task")
        return "GROQ_API_KEY not configured"

    try:
        from groq import Groq
    except ImportError:
        return "groq package missing"

    from .models import Post, Category, Author
    from .data_journalism import ENTERTAINMENT_ANALYSIS_TOPICS, build_static_entertainment_prompt

    client = Groq(api_key=api_key)
    now = timezone.now()
    date_str = now.strftime("%d %B %Y")
    created = 0

    default_author = Author.objects.filter(slug="clinton-nwachukwu").first()
    entertainment_cat = (
        Category.objects.filter(name__icontains="entertain").first()
        or Category.objects.filter(name__icontains="arts").first()
        or Category.objects.filter(name__icontains="news").first()
        or Category.objects.first()
    )

    def call_llm(prompt):
        try:
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.error("Groq API error: %s", exc)
            return None

    def parse_response(raw):
        summary, analysis, html = "", "", raw
        if "---" in raw:
            head, body = raw.split("---", 1)
            html = body.strip()
            current_key = None
            buf = []
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
        return summary, analysis, html

    def recently_generated(tag_snippet, days):
        cutoff = now - timedelta(days=days)
        return Post.objects.filter(
            updated_by="entertainment-auto",
            tags__icontains=tag_snippet,
            date_created__gte=cutoff,
        ).exists()

    def save_draft(title, body, summary, analysis, tags):
        post = Post.objects.create(
            title=title,
            body=body,
            summary=summary or title,
            analysis=analysis or None,
            tags=tags,
            source="api",
            is_published=False,
            updated_by="entertainment-auto",
            author=default_author,
        )
        if entertainment_cat:
            post.categories.add(entertainment_cat)
        logger.info("Created entertainment draft: %s", title)
        return post

    # Rotate through topics by day-of-month
    topic_index = now.day % len(ENTERTAINMENT_ANALYSIS_TOPICS)
    topic = ENTERTAINMENT_ANALYSIS_TOPICS[topic_index]

    if not recently_generated(topic["key"], days=topic["frequency_days"]):
        raw = call_llm(build_static_entertainment_prompt(topic, date_str=date_str))
        if raw:
            summary, analysis, html = parse_response(raw)
            save_draft(
                title=f"{topic['title']} — {now.strftime('%B %Y')}",
                body=html,
                summary=summary,
                analysis=analysis,
                tags=f"{topic['tags']}, {topic['key']}",
            )
            created += 1

    return f"Entertainment articles task complete: {created} draft(s) created"
