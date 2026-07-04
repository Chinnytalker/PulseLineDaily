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
                "You're receiving this because you subscribed to PulseLineDaily.",
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
            logger.error("Newsletter SMTP failed for %s: %s: %s", email, type(exc).__name__, exc)

    logger.info("Newsletter task finished: %d/%d sent", sent, len(subscribers))
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
        parse_llm_response,
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

    parse_response = parse_llm_response

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
    from .data_journalism import fetch_rss_stories, fetch_article_body, build_rewrite_prompt, detect_story_category, parse_llm_response

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

    parse_response = parse_llm_response

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
        parse_llm_response,
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

    parse_response = parse_llm_response

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
    if not recently_generated("wc2026 standings", days=1):
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
                    tags="world cup 2026, wc2026 standings, football, group stage, round of 32, sports, standings",
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
    if now.month not in [6, 7] and not recently_generated("premier league", days=3):
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
        parse_llm_response,
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

    parse_response = parse_llm_response

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
            raw = call_llm(build_brent_prompt(data, date_str=date_str))
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
    if not recently_generated("bitcoin", days=7):
        crypto_data = fetch_crypto_prices()
        if crypto_data:
            from .data_journalism import fetch_usd_ngn_rate as _fetch_ngn
            _ngn = _fetch_ngn()
            ngn_rate = _ngn["rate"] if _ngn else None
            raw = call_llm(build_crypto_prompt(crypto_data, date_str, ngn_rate=ngn_rate))
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
    from .data_journalism import POLITICS_ANALYSIS_TOPICS, build_static_politics_prompt, parse_llm_response

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

    parse_response = parse_llm_response

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
        raw = call_llm(build_static_politics_prompt(topic, date_str=now.strftime("%d %B %Y")))
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
    from .data_journalism import (
        ENTERTAINMENT_ANALYSIS_TOPICS,
        build_static_entertainment_prompt,
        fetch_nigeria_music_charts,
        parse_llm_response,
    )

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

    parse_response = parse_llm_response

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
        # For afrobeats, fetch live Apple Music Nigeria chart data
        chart_data = None
        if topic["key"] == "afrobeats_weekly":
            charts = fetch_nigeria_music_charts(limit=10)
            if charts:
                chart_data = "\n".join(
                    f"{c['rank']}. {c['artist']} — \"{c['song']}\""
                    for c in charts
                )
                logger.info("Fetched %d songs from Apple Music Nigeria", len(charts))
            else:
                logger.warning("Music chart fetch returned empty — LLM will generate without chart data")
                chart_data = "(Chart data unavailable — write about recent Afrobeats trends generally without naming specific songs as 'this week's chart toppers')"

        raw = call_llm(build_static_entertainment_prompt(topic, date_str=date_str, chart_data=chart_data))
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


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def generate_wc2026_match_reports(self):
    """
    Poll TheSportsDB every 30 minutes for newly completed WC2026 matches and write a
    full match report article for each one not yet covered.
    Uses TheSportsDB event ID stored in post.link as dedup key.
    Saves as unpublished draft for rapid review.
    """
    from django.conf import settings
    from django.utils import timezone

    api_key = getattr(settings, 'GROQ_API_KEY', '')
    if not api_key:
        return "GROQ_API_KEY not configured"

    try:
        from groq import Groq
    except ImportError:
        return "groq package missing"

    from .models import Post, Category, Author
    from .data_journalism import (
        fetch_wc2026_completed_matches,
        build_match_result_prompt,
        parse_llm_response,
    )

    import re as _re

    client = Groq(api_key=api_key)
    now = timezone.now()
    date_str = now.strftime("%d %B %Y")
    created = 0

    default_author = Author.objects.filter(slug="clinton-nwachukwu").first()
    sports_cat = (
        Category.objects.filter(name__icontains="sport").first()
        or Category.objects.first()
    )

    def call_llm(prompt):
        try:
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=2500,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.error("Groq API error (WC2026 match reports): %s", exc)
            return None

    matches = fetch_wc2026_completed_matches(days_back=1)
    if not matches:
        return "WC2026 match reports: no completed matches found"

    for match in matches:
        match_id = match.get("id", "")
        if not match_id:
            continue

        dedup_link = f"tsdb-wc2026-match-{match_id}"

        # Skip if we already wrote a report for this match
        if Post.objects.filter(link=dedup_link).exists():
            logger.debug("WC2026 match already covered: %s vs %s", match["home"], match["away"])
            continue

        home = match["home"]
        away = match["away"]
        hs = match["home_score"]
        as_ = match["away_score"]

        if not home or not away:
            continue

        prompt = build_match_result_prompt(match, date_str)
        raw = call_llm(prompt)
        if not raw:
            continue

        # Extract HEADLINE if LLM provided one
        seo_title = f"{home} {hs}–{as_} {away}: World Cup 2026 Match Report"
        headline_match = _re.search(r'^HEADLINE:\s*(.+)', raw, _re.MULTILINE | _re.IGNORECASE)
        if headline_match:
            candidate = headline_match.group(1).strip()[:200]
            if len(candidate) > 15:
                seo_title = candidate
            raw = _re.sub(r'^HEADLINE:.*\n?', '', raw, flags=_re.MULTILINE | _re.IGNORECASE).strip()

        summary, analysis, html_body = parse_llm_response(raw)

        if not html_body or len(html_body.strip()) < 50:
            logger.warning("WC2026 match report — empty body for %s vs %s", home, away)
            continue

        if not summary:
            summary = seo_title[:160]

        stage = match.get("stage", "World Cup 2026")
        tags = (
            f"world cup 2026, football, {home.lower()}, {away.lower()}, "
            f"match report, {stage.lower()}, wc2026, sports"
        )

        try:
            post = Post.objects.create(
                title=seo_title,
                body=html_body,
                summary=summary,
                analysis=analysis or None,
                link=dedup_link,
                source="api",
                source_domain="thesportsdb.com",
                is_published=False,
                updated_by="wc2026-auto",
                author=default_author,
                tags=tags,
            )
            if sports_cat:
                post.categories.add(sports_cat)
            created += 1
            logger.info(
                "WC2026 match report created: %s %d–%d %s [%s]",
                home, hs, as_, away, stage,
            )
        except Exception as exc:
            logger.error("WC2026 match report save failed [%s vs %s]: %s", home, away, exc)

    return f"WC2026 match reports: {created} new article(s) created"


@shared_task(name='projectApp.tasks.notify_admin_new_drafts')
def notify_admin_new_drafts():
    """
    Check for unpublished draft articles created in the last 65 minutes
    and email a summary to the admin. Runs every hour via Celery Beat.
    """
    from django.conf import settings
    from django.core.mail import send_mail
    from django.utils import timezone
    from datetime import timedelta
    from .models import Post

    admin_email = getattr(settings, 'ADMIN_NOTIFICATION_EMAIL', '')
    if not admin_email:
        logger.warning("ADMIN_NOTIFICATION_EMAIL not set — skipping draft notification")
        return "ADMIN_NOTIFICATION_EMAIL not configured"

    since = timezone.now() - timedelta(minutes=65)
    new_drafts = list(
        Post.objects.filter(is_published=False, date_created__gte=since)
        .order_by('date_created')
        .values('id', 'title', 'updated_by', 'date_created')
    )

    if not new_drafts:
        return "No new drafts to notify"

    base_url = getattr(settings, 'SITE_URL', 'https://www.pulselinedaily.com').rstrip('/')
    lines = [f"{len(new_drafts)} new article draft(s) are waiting for review:\n"]
    for d in new_drafts:
        source = d['updated_by'] or 'unknown'
        admin_link = f"{base_url}/admin/projectApp/post/{d['id']}/change/"
        lines.append(f"• [{source}] {d['title'][:90]}")
        lines.append(f"  {admin_link}\n")

    lines += [
        "──────────────────────────────",
        f"View all unpublished drafts:",
        f"{base_url}/admin/projectApp/post/?is_published__exact=0",
    ]

    body = "\n".join(lines)
    subject = f"PulseLineDaily — {len(new_drafts)} new draft(s) ready for review"

    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[admin_email],
            fail_silently=False,
        )
        logger.info("Draft notification sent: %d draft(s)", len(new_drafts))
        return f"Draft notification sent: {len(new_drafts)} drafts"
    except Exception as exc:
        logger.error("Draft notification email failed: %s", exc)
        return f"Email failed: {exc}"


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def scrape_government_agencies(self):
    """
    Scrape press releases from 51 Nigerian government agency websites.
    Runs 3×/day (7am, 1pm, 7pm). Each agency has a 12-hour cooldown so it is
    scraped at most twice a day. Each new item is rewritten by Groq into an
    original article and saved as an unpublished draft for human review.
    """
    import time as _time
    from django.conf import settings
    from django.db.models import Q
    from django.utils import timezone
    from datetime import timedelta
    from .models import GovernmentAgency, Post, Category, Author
    from .gov_scraper import scrape_agency, fetch_gov_content
    from .data_journalism import parse_llm_response

    api_key = getattr(settings, "GROQ_API_KEY", "")
    if not api_key:
        logger.warning("GROQ_API_KEY not set — skipping gov agency scraper")
        return "GROQ_API_KEY not configured"

    try:
        from groq import Groq
    except ImportError:
        logger.error("groq package not installed")
        return "groq package missing"

    client = Groq(api_key=api_key)
    now = timezone.now()
    date_str = now.strftime("%d %B %Y")
    # Per-agency cooldown: 4 hours — allows 6 scrape windows per day
    cooldown_ago = now - timedelta(hours=4)
    MAX_PER_RUN = 10

    agencies = GovernmentAgency.objects.filter(
        is_active=True
    ).filter(
        Q(last_scraped_at__isnull=True) | Q(last_scraped_at__lte=cooldown_ago)
    ).order_by('priority', 'last_scraped_at')

    if not agencies.exists():
        return "Gov scraper: all agencies scraped recently, nothing to do"

    gov_category = (
        Category.objects.filter(name__icontains='government').first()
        or Category.objects.filter(name__icontains='politics').first()
        or Category.objects.filter(name__icontains='news').first()
        or Category.objects.first()
    )

    default_author = Author.objects.filter(slug="clinton-nwachukwu").first()
    sector_labels = dict(GovernmentAgency.SECTOR_CHOICES)

    import re as _re
    _GOV_STOPWORDS = {
        "a", "an", "the", "is", "are", "was", "were", "of", "in", "on", "at",
        "to", "for", "with", "and", "or", "but", "as", "by", "from", "that",
        "this", "it", "its", "be", "has", "have", "had", "will", "not", "no",
        "over", "than", "more", "after", "about", "been", "into", "nigeria",
        "nigerian", "federal", "national", "government", "says", "their",
    }

    def _kw(text):
        return {w for w in _re.findall(r'\b[a-z]{4,}\b', text.lower()) if w not in _GOV_STOPWORDS}

    def is_too_old(item, max_age_days=7):
        from datetime import datetime as _dt, timezone as _tz
        cutoff = now - timedelta(days=max_age_days)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=_tz.utc)

        pub_iso = item.get('published_iso')
        if pub_iso:
            try:
                pub_dt = _dt.fromisoformat(pub_iso)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=_tz.utc)
                return pub_dt < cutoff
            except Exception:
                pass

        # Fall back to year/month in the URL path (e.g. /2024/11/)
        match = _re.search(r'/(\d{4})[/_-](\d{1,2})', item.get('url', ''))
        if match:
            try:
                import calendar as _cal
                yr, mo = int(match.group(1)), int(match.group(2))
                last_day = _cal.monthrange(yr, mo)[1]
                item_date = _dt(yr, mo, last_day, tzinfo=_tz.utc)
                return item_date < cutoff
            except (ValueError, TypeError):
                pass

        return None  # date unknown — caller decides

    def similar_title_exists(title):
        kw = _kw(title)
        if len(kw) < 3:
            return False
        cutoff = now - timedelta(days=7)
        recent = Post.objects.filter(
            updated_by='gov-auto',
            date_created__gte=cutoff,
        ).values_list('title', flat=True)[:300]
        for existing in recent:
            if len(kw & _kw(existing)) >= 3:
                return True
        return False

    def call_llm(prompt):
        try:
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.error("Groq API error (gov scraper): %s", exc)
            return None

    def build_gov_prompt(agency, item, body_text, date_str, has_known_date=True):
        sector_label = sector_labels.get(agency.sector, agency.sector)
        source_excerpt = body_text or item.get('summary') or item.get('title')
        date_warning = (
            "\n⚠️ NO PUBLICATION DATE FOUND — This content has no verifiable date in the URL or feed. "
            "Be EXTRA strict: reject unless the content describes a clearly recent, specific named event "
            "with verifiable details. Evergreen or undated content must be rejected.\n"
            if not has_known_date else ""
        )
        return f"""You are a senior Nigerian journalist and editor at PulseLineDaily, a leading Nigerian news platform.

TODAY'S DATE: {date_str}
{date_warning}
A Nigerian government agency has shared the content below. Before writing anything, make a hard editorial judgement:

REJECT THIS CONTENT (respond only with SKIP: <reason>) if ANY of the following are true:
- It describes a service, portal, or programme that has existed for a long time with no NEW development announced
- It is generic promotional or informational content with no specific newsworthy event
- It reads like an evergreen "about us", FAQ, or awareness page — not a news announcement
- The source content is too thin or vague to write a factual article without inventing details
- There is no clear answer to "what specific thing happened that makes this news right now?"

ONLY WRITE THE ARTICLE if it reports a SPECIFIC, RECENT event: a new decision, regulation, crackdown, data/figures released, appointment, fine issued, policy launched, contract awarded, or a direct response to a public issue.

SOURCE AGENCY: {agency.name} ({agency.acronym}) — {sector_label} sector
ORIGINAL HEADLINE: {item.get('title', '')}
SOURCE URL: {item.get('url', '')}
SOURCE CONTENT:
{source_excerpt[:2500]}

IF YOU DECIDE TO WRITE THE ARTICLE — follow these strict rules:

WHAT YOU CANNOT FABRICATE (no fake news):
- Do NOT invent specific figures, fines, dates, names, or statistics that are not in the source
- Do NOT fabricate or paraphrase quotes — only include a quote if it appears word-for-word in the source
- Do NOT attribute specific claims to the agency unless they appear in the source
- Do NOT write "the agency said X" unless X is explicitly in the source content

WHAT YOU CAN AND MUST ADD (depth and context):
- You MUST add context about why this matters to ordinary Nigerians — draw on your general knowledge of Nigeria's economy, telecoms sector, business climate, or regulatory history
- You MUST explain the broader implications: what problem does this solve? Who benefits? Who is affected?
- You CAN reference general background (e.g. "Nigeria has over 220 million mobile subscribers" or "this is the third regulatory action against network operators this year") — clearly frame these as context, not as claims from the source
- You SHOULD include a forward-looking paragraph: what should Nigerians watch for next?

WRITING RULES (journalism quality):
- Open with the IMPACT or ACTION — not the agency name
- First paragraph answers: WHO did WHAT, and why does it matter to Nigerians RIGHT NOW?
- Use subheadings to break the article into clear sections
- Short paragraphs, active voice, plain English
- Length: 550–800 words — substantial and informative, not thin

SEO HEADLINE RULES:
- Write a headline that is 55–70 characters, includes the key search term, and tells the reader exactly what happened
- Good: "NCC Fines MTN N1bn Over Subscriber Data Breach" | "FIRS Extends Tax Filing Deadline to August for Small Businesses"
- Bad: "NCC Introduces E-Services Portal" | "Government Launches New Initiative" | "Agency Releases Statement on Policy"

OUTPUT FORMAT (if writing the article) — exactly as shown, no deviation:
HEADLINE: <SEO headline 55–70 chars — specific, action-driven, includes key names/figures>
SUMMARY: <2–3 sentences, 250–350 characters — first sentence: the most impactful specific fact from this press release; second sentence: what it means for Nigerians or the sector; do not just restate the headline>
ANALYSIS: <two sentences — the significance for Nigeria's economy or citizens, and what it signals going forward>
---
<article HTML — use <h2>, <h3>, <p>, <ul>, <li> — structure the article with clear subheadings for each section>

SUGGESTED ARTICLE STRUCTURE:
<h2> — mirrors the headline
Opening paragraph — the core news fact
<h3>What Happened</h3> — detailed breakdown of the announcement, decision, or action (source facts only)
<h3>What This Means for Nigerians</h3> — impact on citizens, businesses, or consumers (your analysis + context)
<h3>Background</h3> — relevant context about the agency, sector, or issue (general knowledge, clearly framed)
<h3>What to Watch</h3> — forward-looking paragraph: next steps, deadlines, what to expect

OUTPUT FORMAT (if rejecting):
SKIP: <one sentence explaining why this is not genuinely new or specific news>"""

    total_created = 0
    agencies_processed = 0
    thirty_days_ago = now - timedelta(days=30)

    for agency in agencies:
        if total_created >= MAX_PER_RUN:
            break

        try:
            items = scrape_agency(agency)
            created_for_agency = 0
            # Short label for source_domain field (max_length=100)
            agency_domain = agency.website.replace('https://', '').replace('http://', '').rstrip('/')[:100]

            for item in items:
                if total_created >= MAX_PER_RUN:
                    break

                url = (item.get('url') or '').strip()[:500]
                title = (item.get('title') or '').strip()[:200]

                if not url or not title or len(title) < 10:
                    continue

                # Skip if we already have an article from this exact URL in the last 30 days
                if Post.objects.filter(link=url, date_created__gte=thirty_days_ago).exists():
                    logger.debug("Gov scraper — recently covered URL: %s", url)
                    continue

                has_known_date = bool(item.get('published_iso')) or bool(
                    _re.search(r'/(\d{4})[/_-](\d{1,2})', url)
                )

                # Skip if item is older than 2 days
                age_check = is_too_old(item, max_age_days=2)
                if age_check is True:
                    logger.debug("Gov scraper — stale item skipped: %s", title[:80])
                    continue

                # Skip if a near-identical title was already published in the last 7 days
                if similar_title_exists(title):
                    logger.debug("Gov scraper — similar title already covered: %s", title[:80])
                    continue

                # Fetch full content — handles HTML pages, PDFs, and skips images
                body_text = fetch_gov_content(url, max_chars=2500)

                prompt = build_gov_prompt(agency, item, body_text, date_str, has_known_date=has_known_date)
                raw = call_llm(prompt)
                if not raw:
                    continue

                # LLM editorial rejection — not genuinely new or specific news
                if raw.strip().upper().startswith('SKIP:'):
                    reason = raw.strip()[5:].strip()[:120]
                    logger.info("Gov scraper — LLM rejected '%s': %s", title[:60], reason)
                    continue

                # Extract SEO headline if LLM provided one
                seo_title = title
                headline_match = _re.search(r'^HEADLINE:\s*(.+)', raw, _re.MULTILINE | _re.IGNORECASE)
                if headline_match:
                    candidate = headline_match.group(1).strip()[:200]
                    if len(candidate) > 15:
                        seo_title = candidate
                    raw = _re.sub(r'^HEADLINE:.*\n?', '', raw, flags=_re.MULTILINE | _re.IGNORECASE).strip()

                summary, analysis, html_body = parse_llm_response(raw)

                # Guard: skip if LLM returned no usable body
                if not html_body or len(html_body.strip()) < 50:
                    logger.warning("Gov scraper — LLM returned empty body for: %s", title[:80])
                    continue

                if not summary:
                    summary = seo_title[:160]

                sector_label = sector_labels.get(agency.sector, agency.sector)

                try:
                    post = Post.objects.create(
                        title=seo_title,
                        body=html_body,
                        summary=summary,
                        analysis=analysis or None,
                        link=url,
                        source='scrape',
                        source_domain=agency_domain,
                        is_published=False,
                        updated_by='gov-auto',
                        author=default_author,
                        tags=(
                            f"nigeria, government, press release, {agency.acronym.lower()}, "
                            f"{agency.name.lower()}, {sector_label.lower()}"
                        ),
                    )
                    if gov_category:
                        post.categories.add(gov_category)
                    created_for_agency += 1
                    total_created += 1
                    logger.info("Gov scraper — created article: [%s] %s", agency.acronym, title[:80])
                except Exception as exc:
                    logger.error("Gov scraper — save failed [%s]: %s", title[:80], exc)

                _time.sleep(1)

            agency.last_scraped_at = now
            agency.save(update_fields=['last_scraped_at'])
            agencies_processed += 1

            if created_for_agency:
                logger.info("Gov scraper — %s: %d new article(s)", agency.acronym, created_for_agency)

            # Brief pause between agencies to avoid back-to-back LLM bursts
            _time.sleep(3)

        except Exception as exc:
            logger.error("Gov scraper — agency %s failed: %s", agency.name, exc)

    return (
        f"Gov scraper complete: {total_created} original article(s) "
        f"from {agencies_processed} agencies"
    )


@shared_task(bind=True, max_retries=2, default_retry_delay=120)
def scrape_cbn_news(self):
    """
    Scrape CBN's JSON APIs for recent press releases, circulars, and notices.
    Only processes items published in the last 2 days — never touches old content.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    import requests as _req
    from django.conf import settings
    from django.utils import timezone
    from .models import Post, Category, Author
    from .gov_scraper import fetch_gov_content
    from .data_journalism import parse_llm_response

    api_key = getattr(settings, 'GROQ_API_KEY', '')
    if not api_key:
        return "GROQ_API_KEY not configured"

    try:
        from groq import Groq
    except ImportError:
        return "groq package missing"

    _BASE = 'https://www.cbn.gov.ng'
    _HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': _BASE}
    _APIS = [
        ('press-release', f'{_BASE}/api/GetAllPressReleases', 'CBN Press Release'),
        ('circular',      f'{_BASE}/api/GetAllCirculars',     'CBN Circular'),
        ('notice',        f'{_BASE}/api/GetAllNotices',       'CBN Notice'),
    ]
    DAYS_BACK = 2
    MAX_PER_RUN = 6

    client = Groq(api_key=api_key)
    now = timezone.now()
    date_str = now.strftime("%d %B %Y")
    cutoff = (_dt.now(_tz.utc) - _td(days=DAYS_BACK)).replace(hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago = now - _td(days=30)

    finance_cat = (
        Category.objects.filter(name__icontains='financ').first()
        or Category.objects.filter(name__icontains='econom').first()
        or Category.objects.first()
    )
    default_author = Author.objects.filter(slug='clinton-nwachukwu').first()

    def _parse_date(s):
        try:
            return _dt.strptime(s.strip(), '%d/%m/%Y').replace(tzinfo=_tz.utc)
        except Exception:
            return None

    def call_llm(prompt):
        try:
            resp = client.chat.completions.create(
                model='llama-3.3-70b-versatile',
                max_tokens=2500,
                messages=[{'role': 'user', 'content': prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.error("Groq API error (CBN scraper): %s", exc)
            return None

    _SEARCH_FEEDS = [
        ('businessday.ng',      'https://businessday.ng/feed/'),
        ('punchng.com',         'https://punchng.com/feed/'),
        ('premiumtimesng.com',  'https://www.premiumtimesng.com/feed/'),
        ('vanguardngr.com',     'https://www.vanguardngr.com/feed/'),
    ]
    _CBN_STOPWORDS = {
        'central', 'bank', 'nigeria', 'nigerian', 'press', 'release',
        'circular', 'notice', 'from', 'with', 'their', 'that', 'this',
        'have', 'been', 'will', 'which', 'also', 'they', 'about',
    }

    def _find_news_coverage(title):
        """Search Nigerian news RSS feeds for coverage of this CBN announcement.
        Returns article text (str) or empty string."""
        import re as _re2
        import feedparser as _fp
        kw = set(_re2.findall(r'\b\w{4,}\b', title.lower())) - _CBN_STOPWORDS
        if len(kw) < 2:
            return ''
        for domain, feed_url in _SEARCH_FEEDS:
            try:
                feed = _fp.parse(feed_url)
                for entry in feed.entries[:40]:
                    entry_title = getattr(entry, 'title', '').lower()
                    entry_kw = set(_re2.findall(r'\b\w{4,}\b', entry_title))
                    if len(kw & entry_kw) >= 3:
                        article_url = getattr(entry, 'link', '')
                        if article_url:
                            text = fetch_gov_content(article_url, max_chars=4000)
                            if text and len(text) > 200:
                                logger.info("CBN scraper — news coverage from %s: %s", domain, title[:60])
                                return text
            except Exception:
                pass
        return ''

    def build_cbn_prompt(item, body_text, doc_type):
        title = (item.get('title') or '').strip()
        ref = (item.get('refNo') or '').strip()
        doc_date = (item.get('documentDate') or '').strip()
        description = (item.get('description') or '').strip()
        keywords = (item.get('keywords') or '').strip()

        if body_text and len(body_text.strip()) > 100:
            source_block = f"NEWS COVERAGE (full article text from Nigerian media):\n{body_text[:4000]}"
            source_note = (
                "Use the news coverage above as your PRIMARY source. Every specific fact, name, figure, "
                "and institution mentioned in it may be included. Do not invent additional details."
            )
        else:
            meta_parts = [p for p in [description, keywords] if p]
            meta_str = '\n'.join(meta_parts) if meta_parts else '(none)'
            source_block = f"AVAILABLE METADATA:\n{meta_str}"
            source_note = (
                "The PDF could not be retrieved directly. Write a complete, authoritative article using:\n"
                "1. The specific facts in the TITLE as verified CBN announcements — treat them as confirmed\n"
                "2. Your knowledge of CBN policies, Nigerian banking regulation, and the financial sector\n"
                "   to provide BACKGROUND, CONTEXT, and IMPLICATIONS — this is essential journalism\n"
                "3. Do NOT invent new specific figures, names, or dates not stated in the title/metadata\n"
                "4. Write confidently — do not hedge with 'reportedly' or 'it is believed'"
            )

        return f"""You are a senior financial journalist at PulseLineDaily, Nigeria's leading digital news outlet.

TODAY'S DATE: {date_str}
CBN DOCUMENT DATE: {doc_date}
REF: {ref}
TITLE: {title}
TYPE: {doc_type}
KEYWORDS: {keywords}

{source_block}

EDITORIAL GATE — REJECT (respond only with SKIP: <reason>) if:
- Purely routine scheduled item with zero policy or public-impact (e.g., only an auction calendar)
- Genuinely too narrow/technical for any general-audience article

WRITE the article if it covers: interest rate or exchange rate decision, bank licence action,
public warning or consumer alert, new regulation, economic data, or any CBN action with direct
public or market impact.

{source_note}

OUTPUT FORMAT (exactly as shown):
SUMMARY: <2–3 punchy sentences, max 250 chars — state the specific CBN action clearly>
ANALYSIS: <2 sharp sentences on what this signals for the Nigerian economy or banking sector>
---
<full HTML article body starting with <h2>>

ARTICLE REQUIREMENTS:
- Pure HTML: <h2>, <h3>, <p>, <strong>, <ul>, <li> — no <html>/<body>/<head> tags
- <h2>: specific, action-driven headline naming what CBN did
- Opening <p>: the exact CBN action — what was done, announced, or decided, and when
- <h3>What This Means for Nigerians</h3>: plain-English impact on ordinary people, depositors, businesses
- <h3>Why CBN Acted</h3>: background and regulatory reasoning behind this type of action
- <h3>Key Details</h3>: all specific figures, institutions, dates from the source
- <h3>What Happens Next</h3>: next steps, timelines, what stakeholders should watch
- Closing <p>: broader significance for the Nigerian financial sector
- Length: 500–650 words | Tone: authoritative, factual, Nigerian financial journalism voice
- Attribute to the Central Bank of Nigeria

IF REJECTING:
SKIP: <one line reason>"""

    created = 0

    for doc_slug, api_url, doc_label in _APIS:
        if created >= MAX_PER_RUN:
            break

        try:
            r = _req.get(api_url, headers=_HEADERS, timeout=12)
            r.raise_for_status()
            items = r.json()
        except Exception as exc:
            logger.warning("CBN API fetch failed [%s]: %s", doc_label, exc)
            continue

        for item in items:
            if created >= MAX_PER_RUN:
                break

            doc_date_str = item.get('documentDate', '')
            if doc_date_str:
                parsed_date = _parse_date(doc_date_str)
                if parsed_date and parsed_date < cutoff:
                    break  # API is newest-first — stop at first old item

            title = (item.get('title') or '').strip()
            raw_link = (item.get('link') or '').strip()
            if not title or not raw_link:
                continue

            full_url = raw_link if raw_link.startswith('http') else _BASE + raw_link
            dedup_link = full_url[:500]

            if Post.objects.filter(link=dedup_link, date_created__gte=thirty_days_ago).exists():
                logger.debug("CBN scraper — already covered: %s", title[:80])
                continue

            # CBN PDFs are Cloudflare-protected; search Nigerian news sites for coverage first
            body_text = ''
            if full_url.lower().endswith('.pdf'):
                body_text = _find_news_coverage(title)
                if not body_text:
                    logger.debug("CBN scraper — no news coverage found, using metadata: %s", title[:60])
            else:
                body_text = fetch_gov_content(full_url, max_chars=3000)

            prompt = build_cbn_prompt(item, body_text, doc_label)
            raw = call_llm(prompt)
            if not raw:
                continue

            if raw.strip().upper().startswith('SKIP:'):
                logger.info("CBN scraper — LLM rejected '%s': %s", title[:60], raw[5:].strip()[:100])
                continue

            summary, analysis, html_body = parse_llm_response(raw)
            if not html_body or len(html_body.strip()) < 50:
                logger.warning("CBN scraper — empty body for: %s", title[:80])
                continue

            if not summary:
                summary = title[:160]

            tags = f"cbn, central bank of nigeria, {doc_slug}, financial policy, nigerian economy"

            try:
                post = Post.objects.create(
                    title=title[:200],
                    body=html_body,
                    summary=summary,
                    analysis=analysis or None,
                    link=dedup_link,
                    source='scrape',
                    source_domain='cbn.gov.ng',
                    is_published=False,
                    updated_by='cbn-auto',
                    author=default_author,
                    tags=tags,
                )
                if finance_cat:
                    post.categories.add(finance_cat)
                created += 1
                logger.info("CBN scraper — created draft: %s", title[:80])
            except Exception as exc:
                logger.error("CBN scraper — save failed [%s]: %s", title[:80], exc)

    return f"CBN scraper: {created} new article(s) created"

