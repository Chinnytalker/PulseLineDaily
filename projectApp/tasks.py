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

        import re as _re_rss
        # Extract LLM-generated SEO headline
        seo_title = story["title"][:195]
        headline_match = _re_rss.search(r'^HEADLINE:\s*(.+)', raw, _re_rss.MULTILINE | _re_rss.IGNORECASE)
        if headline_match:
            candidate = headline_match.group(1).strip()[:195]
            if len(candidate) > 15:
                seo_title = candidate
            raw = _re_rss.sub(r'^HEADLINE:.*\n?', '', raw, flags=_re_rss.MULTILINE | _re_rss.IGNORECASE).strip()

        summary, analysis, html = parse_response(raw)
        if not summary:
            summary = seo_title[:160]

        resolved_cat = detect_story_category(story["title"], story["excerpt"], story["category"])
        cat = best_category(resolved_cat)

        try:
            post = Post.objects.create(
                title=seo_title,
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
            logger.info("Created RSS rewrite draft: %s", seo_title)
            created += 1
        except Exception as exc:
            logger.error("Failed to save RSS draft [%s]: %s", seo_title[:80], exc)

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
        return f"""You are a senior Nigerian journalist and editor at PulseLineDaily, a leading Nigerian news platform published on Google News and monetised via AdSense.

TODAY'S DATE: {date_str}
{date_warning}
A Nigerian government agency has shared the content below. Make a hard editorial judgement before writing anything.

REJECT (respond only with SKIP: <reason>) if ANY of the following are true:
- No new development — describes an existing service, portal, or programme with no specific new action
- Generic promotional content — "awareness", "about us", FAQ, or motivational statement
- Too thin to write a factual article without inventing details
- No clear answer to: "what specific thing happened that makes this news today?"

WRITE ONLY if it reports: a new decision, regulation, fine, crackdown, data/statistics released, appointment, policy launched, contract awarded, licence action, or a direct government response to a public issue.

SOURCE AGENCY: {agency.name} ({agency.acronym}) — {sector_label} sector
ORIGINAL HEADLINE: {item.get('title', '')}
SOURCE URL: {item.get('url', '')}
SOURCE CONTENT:
{source_excerpt[:2500]}

STRICT FACTUAL RULES:
- Do NOT invent figures, fines, dates, names, or statistics not in the source
- Do NOT fabricate or paraphrase quotes — only use a quote if it appears word-for-word in the source
- Do NOT write "the agency said X" unless X is explicitly in the source

DEPTH AND CONTEXT — you must add these:
- Explain WHY this matters to ordinary Nigerians — draw on your knowledge of Nigeria's economy, regulation, or sector history
- State the broader implications: who benefits, who is affected, what problem this solves or creates
- Add factual background (e.g. "Nigeria's electricity sector has seen..." or "This is the second fine this quarter...") — clearly frame as context, not as claims from the source

WRITING QUALITY — what makes this a great article, not AI filler:
- Open with the IMPACT or ACTION — never start with the agency name or "The [Agency Name] has..."
- Lead paragraph: WHO did WHAT + the most important specific number or fact + why it matters NOW
- Short paragraphs (2–3 sentences max) — digital journalism, not an essay
- Active voice: "NCC fined MTN ₦1bn" not "MTN was fined ₦1bn by NCC"
- Specific beats vague: "₦750 million penalty" beats "a significant fine"; "12,000 subscribers affected" beats "many customers"
- BANNED openers: "In a significant development", "It is noteworthy that", "Against the backdrop of", "In line with", "It is important to mention", "Furthermore"
- Cut any sentence that doesn't add new information
- Length: 550–750 words — every word earning its place

SEO HEADLINE RULES (HEADLINE: field):
- 55–65 characters — Google truncates beyond ~60; be precise
- Format: [Subject] [Action verb] [Specific detail] — present tense
- Must include: the agency/actor name + the specific action + a key figure/name where possible
- Good: "NCC Fines MTN ₦1bn Over Subscriber Data Breach" | "FIRS Sets August Deadline for SME Tax Returns"
- Bad: "NCC Introduces E-Services Portal" | "Government Makes Major Announcement" | "Agency Takes Action on Policy"
- BANNED words in headlines: "Significant", "Major", "Key", "Important", "New Development", "Update", "Breaking"

OUTPUT FORMAT — if writing the article, exactly as shown:
HEADLINE: <55–65 char SEO title — subject + verb + key detail, present tense>
SUMMARY: <2–3 sentences, 250–350 characters — sentence 1: the most impactful specific fact; sentence 2: what it means for Nigerians or the sector; do NOT restate the headline>
ANALYSIS: <2 sentences — significance for Nigeria's economy or citizens + what it signals going forward>
---
<full HTML article body — do NOT repeat HEADLINE/SUMMARY/ANALYSIS as plain text here>

ARTICLE STRUCTURE:
<h2> mirrors HEADLINE exactly
Opening <p>: the lead — key action, specific figure, immediate significance
<h3>What Happened</h3>: source facts only — decision, figures, institutions, timeline
<h3>What This Means for Nigerians</h3>: your analysis — impact on citizens, businesses, consumers
<h3>Background</h3>: sector context — general knowledge, clearly framed as background
<h3>What to Watch</h3>: next steps, deadlines, what Nigerians should monitor

OUTPUT FORMAT — if rejecting:
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

    import re as _re
    _CBN_STOPWORDS_DEDUP = {
        'central', 'bank', 'nigeria', 'nigerian', 'press', 'release',
        'circular', 'notice', 'cbn', 'from', 'with', 'their', 'that',
        'this', 'have', 'been', 'will', 'which', 'also', 'they', 'about',
        'financial', 'policy', 'regarding', 'further', 'following',
    }

    def _cbn_kw(text):
        return {
            w for w in _re.findall(r'\b[a-z]{4,}\b', text.lower())
            if w not in _CBN_STOPWORDS_DEDUP
        }

    def similar_cbn_topic_exists(title):
        """True if a cbn-auto post with 3+ overlapping keywords exists in the last 7 days."""
        kw = _cbn_kw(title)
        if len(kw) < 3:
            return False
        cutoff_7d = now - _td(days=7)
        recent = Post.objects.filter(
            updated_by='cbn-auto',
            date_created__gte=cutoff_7d,
        ).values_list('title', flat=True)[:300]
        for existing in recent:
            if len(kw & _cbn_kw(existing)) >= 3:
                return True
        return False

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

        return f"""You are a senior financial journalist at PulseLineDaily, Nigeria's leading digital news outlet published on Google News and monetised via AdSense.

TODAY'S DATE: {date_str}
CBN DOCUMENT DATE: {doc_date}
REF: {ref}
TITLE: {title}
TYPE: {doc_type}
KEYWORDS: {keywords}

{source_block}

EDITORIAL GATE — REJECT (respond only with SKIP: <reason>) if:
- Purely routine scheduled item with zero policy or public impact (e.g. only an auction calendar date)
- Genuinely too narrow or technical for any general-audience article

WRITE if it covers: interest rate or MPR decision, exchange rate action, bank licence granted/revoked, public warning or fraud alert, new regulation, economic data release, forex policy, or any CBN action with direct public or market impact.

{source_note}

STRICT FACTUAL RULES:
- Do NOT invent figures, rates, institution names, or dates not in the source
- Do NOT fabricate or paraphrase quotes — only use exact words from the source
- Do NOT write "CBN said X" unless X appears explicitly in the source

WRITING QUALITY — CBN journalism must be authoritative, not robotic:
- Open with the specific action and its most important figure (rate, amount, institution) — not "The Central Bank of Nigeria has..."
- Lead paragraph: the decision + the exact number + why it matters to Nigerians NOW
- Short paragraphs (2–3 sentences) — tight financial journalism
- Active voice: "CBN raised the MPR to 27.5%" not "The MPR was raised by CBN"
- Specific beats vague: "27.5% MPR" beats "a higher rate"; "₦50 billion fine" beats "a substantial penalty"
- BANNED openers: "In a significant development", "It is noteworthy", "Against the backdrop of", "Furthermore", "It is important to mention"
- Length: 500–650 words — every word must add value

SEO HEADLINE RULES (HEADLINE: field):
- 55–65 characters — Google truncates at ~60
- Format: [CBN action verb] [specific detail + figure] — present tense
- Must name the specific action + a key figure, rate, or institution where available
- Good: "CBN Raises MPR to 27.5% as Inflation Hits 32%" | "CBN Revokes Licence of Three Microfinance Banks"
- Bad: "CBN Makes Major Policy Announcement" | "Central Bank Takes Action on Interest Rates" | "CBN Issues Important Circular"
- BANNED: "Significant", "Major", "Key", "Important", "New Development", "Update"

OUTPUT FORMAT — if writing the article, exactly as shown:
HEADLINE: <55–65 char SEO title — CBN action + specific figure/detail, present tense>
SUMMARY: <2–3 punchy sentences, 200–280 characters — sentence 1: the exact CBN action and key figure; sentence 2: what it means for Nigerians, depositors, or the economy; do NOT restate the headline>
ANALYSIS: <2 sharp sentences — what this signals for Nigeria's monetary policy direction and what to watch next>
---
<full HTML article body — do NOT repeat HEADLINE/SUMMARY/ANALYSIS as plain text here>

ARTICLE STRUCTURE:
<h2> mirrors HEADLINE exactly
Opening <p>: the CBN action — specific decision, exact figure, and immediate significance
<h3>What This Means for Nigerians</h3>: plain-English impact — depositors, borrowers, businesses, exchange rate
<h3>Why CBN Acted</h3>: regulatory background and reasoning — your financial knowledge, clearly framed as context
<h3>Key Details</h3>: all specific figures, institutions, dates, reference numbers from the source
<h3>What Happens Next</h3>: next steps, implementation timeline, what markets and stakeholders should watch
Closing <p>: one sharp sentence on broader significance for Nigeria's financial sector

IF REJECTING:
SKIP: <one sentence reason>"""

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
                logger.debug("CBN scraper — already covered (URL): %s", title[:80])
                continue

            if similar_cbn_topic_exists(title):
                logger.info("CBN scraper — similar topic already covered, skipping: %s", title[:80])
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

            # Extract SEO headline if LLM provided one
            seo_title = title[:200]
            headline_match = _re.search(r'^HEADLINE:\s*(.+)', raw, _re.MULTILINE | _re.IGNORECASE)
            if headline_match:
                candidate = headline_match.group(1).strip()[:200]
                if len(candidate) > 15:
                    seo_title = candidate
                raw = _re.sub(r'^HEADLINE:.*\n?', '', raw, flags=_re.MULTILINE | _re.IGNORECASE).strip()

            summary, analysis, html_body = parse_llm_response(raw)
            if not html_body or len(html_body.strip()) < 50:
                logger.warning("CBN scraper — empty body for: %s", title[:80])
                continue

            if not summary:
                summary = seo_title[:160]

            tags = f"cbn, central bank of nigeria, {doc_slug}, financial policy, nigerian economy"

            try:
                post = Post.objects.create(
                    title=seo_title,
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
                logger.info("CBN scraper — created draft: %s", seo_title[:80])
            except Exception as exc:
                logger.error("CBN scraper — save failed [%s]: %s", seo_title[:80], exc)

    return f"CBN scraper: {created} new article(s) created"


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def scrape_statehouse(self):
    """
    Dedicated scraper for statehouse.gov.ng — Nigerian Presidency news & press releases.
    Runs 3×/day. Only processes articles published within the last 2 days.
    Deduplicates by URL. Saves unpublished drafts for human review.
    """
    import re as _re
    import time as _time
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    import requests as _req
    from bs4 import BeautifulSoup as _BS

    from django.conf import settings
    from django.utils import timezone

    from .models import Post, Category, Author
    from .data_journalism import parse_llm_response

    api_key = getattr(settings, 'GROQ_API_KEY', '')
    if not api_key:
        return "GROQ_API_KEY not configured"

    try:
        from groq import Groq
    except ImportError:
        return "groq package missing"

    _HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        )
    }
    _SOURCES = [
        'https://statehouse.gov.ng/category/news/',
        'https://statehouse.gov.ng/category/press-releases/',
    ]
    _BASE = 'https://statehouse.gov.ng'
    _NAV_SKIP = {
        'people', 'presidency', 'category', 'tag', 'page',
        'search', 'feed', 'wp-content', 'wp-includes', 'wp-login',
        'contact', 'about', 'presidential-villa',
    }
    MAX_PER_RUN = 6
    MAX_DAYS = 2

    now = timezone.now()
    date_str = now.strftime("%d %B %Y")
    cutoff = _dt.now(_tz.utc) - _td(days=MAX_DAYS)
    thirty_days_ago = now - _td(days=30)

    client = Groq(api_key=api_key)
    default_author = Author.objects.filter(slug='clinton-nwachukwu').first()
    gov_cat = (
        Category.objects.filter(name__icontains='politics').first()
        or Category.objects.filter(name__icontains='government').first()
        or Category.objects.filter(name__icontains='news').first()
        or Category.objects.first()
    )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def call_llm(prompt):
        try:
            resp = client.chat.completions.create(
                model='llama-3.3-70b-versatile',
                max_tokens=3000,
                messages=[{'role': 'user', 'content': prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.error("Groq API error (statehouse): %s", exc)
            return None

    _MONTHS = {
        'january': 1, 'february': 2, 'march': 3, 'april': 4,
        'may': 5, 'june': 6, 'july': 7, 'august': 8,
        'september': 9, 'october': 10, 'november': 11, 'december': 12,
    }

    def _extract_date(text):
        """Parse 'Month D, YYYY' or 'D Month YYYY' from raw page text."""
        for pat in [
            r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(20\d\d)',
            r'(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December),?\s+(20\d\d)',
        ]:
            m = _re.search(pat, text, _re.IGNORECASE)
            if m:
                try:
                    groups = m.groups()
                    if groups[0].isdigit():
                        day, month_name, year = int(groups[0]), groups[1].lower(), int(groups[2])
                    else:
                        month_name, day, year = groups[0].lower(), int(groups[1]), int(groups[2])
                    mo = _MONTHS.get(month_name)
                    if mo:
                        return _dt(year, mo, day, tzinfo=_tz.utc)
                except (ValueError, TypeError):
                    pass
        return None

    def _collect_article_urls():
        """Scrape both category pages and return a deduplicated list of article URLs."""
        seen = set()
        urls = []
        for source in _SOURCES:
            try:
                resp = _req.get(source, headers=_HEADERS, timeout=15)
                resp.raise_for_status()
                soup = _BS(resp.text, 'html.parser')
                for a in soup.find_all('a', href=True):
                    href = a['href'].strip()
                    if _BASE not in href:
                        continue
                    if href in seen:
                        continue
                    if any(skip in href.lower() for skip in _NAV_SKIP):
                        continue
                    text = a.get_text(strip=True)
                    if len(text) < 15:
                        continue
                    # Must look like a real article slug (has path depth > 1)
                    path = href.replace(_BASE, '').strip('/')
                    if '/' in path:
                        continue  # skip nested paths like /category/news/
                    if not path:
                        continue
                    seen.add(href)
                    urls.append({'title': text, 'url': href})
            except Exception as exc:
                logger.warning("Statehouse category fetch failed (%s): %s", source, exc)
        return urls

    def _fetch_article(url):
        """Fetch an article page. Returns (date, body_text) — either may be None."""
        try:
            resp = _req.get(url, headers=_HEADERS, timeout=12)
            resp.raise_for_status()
            soup = _BS(resp.text, 'html.parser')

            # Extract publication date from visible text (appears near top of page)
            all_text = soup.get_text(separator='\n')
            pub_date = _extract_date(all_text)

            # Extract article body from the dedicated content div
            content_div = (
                soup.find('div', class_='news-detail-content')
                or soup.find('div', class_=_re.compile(r'entry-content|post-content|article-content', _re.I))
            )
            if content_div:
                body_text = content_div.get_text(separator=' ', strip=True)
            else:
                # Fallback: get all paragraph text
                paras = soup.find_all('p')
                body_text = ' '.join(p.get_text(strip=True) for p in paras if len(p.get_text(strip=True)) > 40)

            body_text = _re.sub(r'\s+', ' ', body_text).strip()[:3000]
            return pub_date, body_text
        except Exception as exc:
            logger.warning("Statehouse article fetch failed (%s): %s", url, exc)
            return None, ''

    def build_statehouse_prompt(title, body_text, pub_date_str):
        return f"""You are a senior Nigerian political journalist at PulseLineDaily, Nigeria's leading digital news outlet.

TODAY'S DATE: {date_str}
ARTICLE DATE: {pub_date_str}
SOURCE: State House Nigeria (Official Nigerian Presidency)
ORIGINAL HEADLINE: {title}

SOURCE CONTENT (official Presidency statement):
{body_text[:2800]}

EDITORIAL GATE — respond only with SKIP: <reason> if:
- The content is ceremonial with no policy substance (condolences, birthday wishes, congratulations with no policy angle)
- It is a duplicate of a story already well-covered with no new information
- The content is too thin to write a factual, informative article without fabricating details

WRITE the article if it covers: policy announcements, economic decisions, infrastructure, security, appointments, foreign affairs, government programmes, legislative action, or any Presidential action with real public impact.

JOURNALISM RULES:
- Do NOT fabricate quotes — only include a quote if it appears word-for-word in the source
- Do NOT invent statistics, names, or figures not in the source
- You MAY add context from your knowledge of Nigerian governance and the policy area involved
- Open with the IMPACT — what did the government actually do or decide, and why does it matter?
- Length: 550–750 words

OUTPUT FORMAT (exactly as shown):
HEADLINE: <SEO headline 55–70 chars — specific, names what happened>
SUMMARY: <2–3 sentences, 250–350 characters — the key action and its significance for Nigerians>
ANALYSIS: <2 sentences of editorial analysis — what this signals about the Tinubu administration's direction>
---
<full HTML article body starting with <h2>>

ARTICLE STRUCTURE:
<h2> mirrors the headline
Opening <p>: the key Presidential action — who, what, when, and why it matters
<h3>What Was Announced</h3>: detailed breakdown of the policy/decision (source facts only)
<h3>What This Means for Nigerians</h3>: plain-English implications for citizens, businesses, or communities
<h3>Context and Background</h3>: relevant policy background — use your knowledge, clearly frame as context
<h3>What Happens Next</h3>: next steps, timelines, implementation — what Nigerians should watch for
Closing <p>: one sharp editorial sentence on significance

HTML: pure <h2>, <h3>, <p>, <strong>, <ul>, <li> — no html/body/head/style tags
Attribute to: State House, Abuja

IF REJECTING: SKIP: <one sentence reason>"""

    # ── Main loop ─────────────────────────────────────────────────────────────

    articles = _collect_article_urls()
    if not articles:
        return "Statehouse scraper: no articles found on category pages"

    created = 0

    for item in articles:
        if created >= MAX_PER_RUN:
            break

        url = item['url']
        title = item['title'][:200]

        # URL dedup — skip if already covered in the last 30 days
        if Post.objects.filter(link=url, date_created__gte=thirty_days_ago).exists():
            logger.debug("Statehouse — already covered: %s", title[:60])
            continue

        pub_date, body_text = _fetch_article(url)

        # Date filter — only process articles from the last 2 days
        if pub_date is not None:
            if pub_date < cutoff:
                logger.debug("Statehouse — stale article skipped (%s): %s", pub_date.date(), title[:60])
                continue
            pub_date_str = pub_date.strftime("%d %B %Y")
        else:
            # No date found — allow through (category pages show most recent first)
            pub_date_str = "date not specified"

        if not body_text or len(body_text) < 80:
            logger.debug("Statehouse — empty body, skipping: %s", title[:60])
            continue

        prompt = build_statehouse_prompt(title, body_text, pub_date_str)
        raw = call_llm(prompt)
        if not raw:
            continue

        if raw.strip().upper().startswith('SKIP:'):
            logger.info("Statehouse — LLM rejected '%s': %s", title[:60], raw[5:].strip()[:100])
            continue

        # Extract SEO headline
        seo_title = title
        headline_match = _re.search(r'^HEADLINE:\s*(.+)', raw, _re.MULTILINE | _re.IGNORECASE)
        if headline_match:
            candidate = headline_match.group(1).strip()[:200]
            if len(candidate) > 15:
                seo_title = candidate
            raw = _re.sub(r'^HEADLINE:.*\n?', '', raw, flags=_re.MULTILINE | _re.IGNORECASE).strip()

        summary, analysis, html_body = parse_llm_response(raw)

        if not html_body or len(html_body.strip()) < 50:
            logger.warning("Statehouse — empty body from LLM for: %s", title[:60])
            continue

        if not summary:
            summary = seo_title[:160]

        try:
            post = Post.objects.create(
                title=seo_title,
                body=html_body,
                summary=summary,
                analysis=analysis or None,
                link=url,
                source='scrape',
                source_domain='statehouse.gov.ng',
                is_published=False,
                updated_by='statehouse-auto',
                author=default_author,
                tags=(
                    'nigeria, presidency, state house, president tinubu, '
                    'government, policy, abuja, politics'
                ),
            )
            if gov_cat:
                post.categories.add(gov_cat)
            created += 1
            logger.info("Statehouse — created draft: %s", seo_title[:80])
        except Exception as exc:
            logger.error("Statehouse — save failed [%s]: %s", title[:60], exc)

        _time.sleep(2)

    return f"Statehouse scraper: {created} new article(s) created"


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def scrape_efcc_news(self):
    """
    Dedicated scraper for EFCC (efcc.gov.ng) using their JSON API.
    Runs 4x/day. Only processes articles published within the last 2 days.
    Deduplicates by slug URL. Saves unpublished drafts for human review.
    """
    import re as _re
    import time as _time
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    import requests as _req
    from bs4 import BeautifulSoup as _BS
    from django.conf import settings
    from django.utils import timezone
    from .models import Post, Category, Author
    from .data_journalism import parse_llm_response

    api_key = getattr(settings, 'GROQ_API_KEY', '')
    if not api_key:
        return "GROQ_API_KEY not configured"

    try:
        from groq import Groq
    except ImportError:
        return "groq package missing"

    _API_URL = 'https://efcc.gov.ng/backend/news/list.php?page=1&limit=9&status=published'
    _ARTICLE_BASE = 'https://efcc.gov.ng/News'
    _HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; PulseLineDaily-GovBot/1.0)'}
    DAYS_BACK = 2
    MAX_PER_RUN = 6

    client = Groq(api_key=api_key)
    now = timezone.now()
    date_str = now.strftime("%d %B %Y")
    cutoff = (_dt.now(_tz.utc) - _td(days=DAYS_BACK)).replace(hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago = now - _td(days=30)

    politics_cat = (
        Category.objects.filter(name__icontains='polit').first()
        or Category.objects.filter(name__icontains='article').first()
        or Category.objects.first()
    )
    default_author = Author.objects.filter(slug='clinton-nwachukwu').first()

    def _parse_date(s):
        try:
            return _dt.strptime(s.strip(), '%Y-%m-%d %H:%M:%S').replace(tzinfo=_tz.utc)
        except Exception:
            return None

    def _html_to_text(html, max_chars=3000):
        try:
            soup = _BS(html, 'lxml')
            text = soup.get_text(separator=' ', strip=True)
            return ' '.join(text.split())[:max_chars]
        except Exception:
            return html[:max_chars]

    def call_llm(prompt):
        try:
            resp = client.chat.completions.create(
                model='llama-3.3-70b-versatile',
                max_tokens=2500,
                messages=[{'role': 'user', 'content': prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.error("Groq API error (EFCC scraper): %s", exc)
            return None

    def build_efcc_prompt(title, body_text, pub_date_str):
        return f"""You are a senior Nigerian investigative journalist at PulseLineDaily, Nigeria's leading digital news outlet published on Google News and monetised via AdSense.

TODAY'S DATE: {date_str}
ARTICLE DATE: {pub_date_str}
SOURCE: Economic and Financial Crimes Commission (EFCC)
ORIGINAL HEADLINE: {title}

SOURCE CONTENT (official EFCC report):
{body_text[:2800]}

EDITORIAL GATE — respond only with SKIP: <reason> if:
- The content is purely ceremonial with no news value (general awareness campaigns, staff events)
- It is too thin to write a factual article without inventing details
- It duplicates a story type already heavily covered (e.g. a minor arraignment with no names or figures)

WRITE the article if it covers: court arraignment or conviction, asset forfeiture, major arrest, fraud warning for the public, significant investigation, policy statement, or any EFCC action with clear public or financial impact.

STRICT FACTUAL RULES:
- Do NOT invent names, amounts, court names, or charges not in the source
- Do NOT fabricate quotes — only use exact words from the source
- Named defendants, charge amounts, and court details must come from the source only
- You MAY add context from your knowledge of Nigerian anti-corruption law and financial crime

WRITING QUALITY — EFCC journalism must be punchy, factual, not bureaucratic:
- Open with the charge and the exact amount or key detail — not "The EFCC has..."
- Short paragraphs (2–3 sentences)
- Active voice: "EFCC arraigned Chidi Okonkwo for N500m fraud" not "A suspect was arraigned"
- Specific beats vague: "N986 million AGO fraud" beats "a significant fraud"
- BANNED openers: "In a significant development", "It is noteworthy", "Against the backdrop of", "Furthermore"

SEO HEADLINE RULES (HEADLINE: field):
- 55–65 characters — Google truncates at ~60
- Format: [EFCC action] [suspect/company + amount/crime] — present tense
- Good: "EFCC Arraigns Lagos Firm for N986m AGO Fraud" | "Court Orders Forfeiture of N150m Linked to Ex-Gov"
- Bad: "EFCC Takes Action on Fraud Case" | "Major Anti-Corruption Development"
- BANNED: "Significant", "Major", "Key", "Important", "Update"

OUTPUT FORMAT (exactly as shown):
HEADLINE: <55–65 char SEO title — EFCC action + suspect/amount/crime, present tense>
SUMMARY: <2–3 sentences, 200–280 characters — sentence 1: the specific EFCC action and key figure/charge; sentence 2: what it means for justice/accountability in Nigeria>
ANALYSIS: <2 sentences — what this case signals about EFCC's current enforcement priorities>
---
<full HTML article body — do NOT repeat HEADLINE/SUMMARY/ANALYSIS as plain text>

ARTICLE STRUCTURE:
<h2> mirrors HEADLINE exactly
Opening <p>: the EFCC action — who is charged/convicted/arrested, exact amount or crime, and when
<h3>The Charges</h3>: breakdown of specific counts, sections of law cited, and what the prosecution alleges
<h3>Background to the Case</h3>: how the EFCC investigation began, earlier court proceedings, key context
<h3>What the EFCC Says</h3>: EFCC spokesperson or prosecution position — use only exact quotes from source
<h3>What Happens Next</h3>: next hearing date, bail status, what to watch
Closing <p>: one sharp sentence on the case's significance for anti-corruption efforts in Nigeria

HTML: pure <h2>, <h3>, <p>, <strong>, <ul>, <li> — no html/body/head/style tags
Attribute to: Economic and Financial Crimes Commission (EFCC)

IF REJECTING: SKIP: <one sentence reason>"""

    # ── Fetch articles from EFCC JSON API ─────────────────────────────────────

    try:
        resp = _req.get(_API_URL, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        api_data = resp.json()
    except Exception as exc:
        logger.error("EFCC scraper — API fetch failed: %s", exc)
        return "EFCC scraper: API fetch failed"

    articles = api_data.get('data', [])
    if not articles:
        return "EFCC scraper: no articles returned from API"

    created = 0

    for item in articles:
        if created >= MAX_PER_RUN:
            break

        title = (item.get('title') or '').strip()[:200]
        slug = (item.get('slug') or '').strip()
        content_html = item.get('content') or ''
        created_at_str = item.get('created_at') or ''

        if not title or not slug or not content_html:
            continue

        # Date filter — only process articles from the last 2 days
        pub_date = _parse_date(created_at_str)
        if pub_date is not None and pub_date < cutoff:
            logger.debug("EFCC scraper — stale article skipped (%s): %s", pub_date.date(), title[:60])
            continue

        pub_date_str = pub_date.strftime("%d %B %Y") if pub_date else date_str
        dedup_url = f"{_ARTICLE_BASE}/{slug}"

        # URL dedup — skip if already covered in the last 30 days
        if Post.objects.filter(link=dedup_url, date_created__gte=thirty_days_ago).exists():
            logger.debug("EFCC scraper — already covered: %s", title[:60])
            continue

        body_text = _html_to_text(content_html)
        if len(body_text) < 80:
            logger.debug("EFCC scraper — content too short, skipping: %s", title[:60])
            continue

        prompt = build_efcc_prompt(title, body_text, pub_date_str)
        raw = call_llm(prompt)
        if not raw:
            continue

        if raw.strip().upper().startswith('SKIP:'):
            logger.info("EFCC scraper — LLM rejected '%s': %s", title[:60], raw[5:].strip()[:100])
            continue

        # Extract SEO headline
        seo_title = title
        headline_match = _re.search(r'^HEADLINE:\s*(.+)', raw, _re.MULTILINE | _re.IGNORECASE)
        if headline_match:
            candidate = headline_match.group(1).strip()[:200]
            if len(candidate) > 15:
                seo_title = candidate
            raw = _re.sub(r'^HEADLINE:.*\n?', '', raw, flags=_re.MULTILINE | _re.IGNORECASE).strip()

        summary, analysis, html_body = parse_llm_response(raw)
        if not html_body or len(html_body.strip()) < 50:
            logger.warning("EFCC scraper — empty body for: %s", title[:80])
            continue

        if not summary:
            summary = seo_title[:160]

        try:
            post = Post.objects.create(
                title=seo_title,
                body=html_body,
                summary=summary,
                analysis=analysis or None,
                link=dedup_url,
                source='scrape',
                source_domain='efcc.gov.ng',
                is_published=False,
                updated_by='efcc-auto',
                author=default_author,
                tags='efcc, anti-corruption, fraud, nigeria, financial crimes, prosecution',
            )
            if politics_cat:
                post.categories.add(politics_cat)
            created += 1
            logger.info("EFCC scraper — created draft: %s", seo_title[:80])
        except Exception as exc:
            logger.error("EFCC scraper — save failed [%s]: %s", seo_title[:80], exc)

        _time.sleep(2)

    return f"EFCC scraper: {created} new article(s) created"


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def scrape_nnpc_news(self):
    """
    Dedicated scraper for NNPC Limited using their Strapi CMS JSON API.
    Runs 4x/day. Only processes articles published within the last 2 days.
    Deduplicates by slug URL. Saves unpublished drafts for human review.
    """
    import re as _re
    import time as _time
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    import requests as _req
    from bs4 import BeautifulSoup as _BS
    from django.conf import settings
    from django.utils import timezone
    from .models import Post, Category, Author
    from .data_journalism import parse_llm_response

    api_key = getattr(settings, 'GROQ_API_KEY', '')
    if not api_key:
        return "GROQ_API_KEY not configured"

    try:
        from groq import Groq
    except ImportError:
        return "groq package missing"

    _API_URL = (
        'https://cms1977.nnpcgroup.com/api/posts'
        '?pagination[pageSize]=9&sort=publishedAt:desc&populate=*'
    )
    _ARTICLE_BASE = 'https://nnpcgroup.com/insights'
    _HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; PulseLineDaily-GovBot/1.0)', 'Accept': 'application/json'}
    DAYS_BACK = 2
    MAX_PER_RUN = 6

    client = Groq(api_key=api_key)
    now = timezone.now()
    date_str = now.strftime("%d %B %Y")
    cutoff = (_dt.now(_tz.utc) - _td(days=DAYS_BACK)).replace(hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago = now - _td(days=30)

    energy_cat = (
        Category.objects.filter(name__icontains='energy').first()
        or Category.objects.filter(name__icontains='business').first()
        or Category.objects.filter(name__icontains='article').first()
        or Category.objects.first()
    )
    default_author = Author.objects.filter(slug='clinton-nwachukwu').first()

    def _parse_date(s):
        for fmt in ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d'):
            try:
                return _dt.strptime(s.strip(), fmt).replace(tzinfo=_tz.utc)
            except Exception:
                pass
        return None

    def _html_to_text(html, max_chars=3000):
        try:
            soup = _BS(html, 'lxml')
            text = soup.get_text(separator=' ', strip=True)
            return ' '.join(text.split())[:max_chars]
        except Exception:
            return html[:max_chars]

    def call_llm(prompt):
        try:
            resp = client.chat.completions.create(
                model='llama-3.3-70b-versatile',
                max_tokens=2500,
                messages=[{'role': 'user', 'content': prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.error("Groq API error (NNPC scraper): %s", exc)
            return None

    def build_nnpc_prompt(title, body_text, pub_date_str, category):
        return f"""You are a senior Nigerian energy journalist at PulseLineDaily, Nigeria's leading digital news outlet published on Google News and monetised via AdSense.

TODAY'S DATE: {date_str}
ARTICLE DATE: {pub_date_str}
SOURCE: NNPC Limited (Nigeria's national oil company)
CATEGORY: {category}
ORIGINAL HEADLINE: {title}

SOURCE CONTENT (official NNPC release):
{body_text[:2800]}

EDITORIAL GATE — respond only with SKIP: <reason> if:
- Content is too thin to write a factual article (e.g. only "Download the report below" with no data)
- It is purely ceremonial with no energy, economic, or policy substance
- It is a generic staff event or internal corporate notice with no public relevance

WRITE if it covers: oil/gas production figures, revenue or financial results, new partnerships or contracts, refinery updates, fuel pricing, pipeline or infrastructure news, government policy on energy, environmental initiatives with real detail, or any NNPC action with direct public or market impact.

STRICT FACTUAL RULES:
- Do NOT invent figures, partner names, contract values, or dates not in the source
- Do NOT fabricate quotes — only use exact words from the source
- You MAY add context from your knowledge of Nigeria's oil and gas sector

WRITING QUALITY — energy journalism must be specific and data-led:
- Open with the most important number or decision — production figure, contract value, new policy
- Short paragraphs (2–3 sentences), active voice
- Specific beats vague: "1.8 million barrels/day" beats "increased production"
- BANNED openers: "In a significant development", "It is noteworthy", "Against the backdrop of", "Furthermore"
- Length: 500–650 words

SEO HEADLINE RULES (HEADLINE: field):
- 55–65 characters — Google truncates at ~60
- Format: [NNPC action/result] [key figure or detail] — present tense
- Good: "NNPC Posts ₦2.1trn Revenue in Q1 2026" | "NNPC, TotalEnergies Sign Gas Emission Deal"
- Bad: "NNPC Makes Major Announcement" | "Significant Development in Nigerian Oil Sector"
- BANNED: "Significant", "Major", "Key", "Important", "Update", "New Development"

OUTPUT FORMAT (exactly as shown):
HEADLINE: <55–65 char SEO title — NNPC action + key figure/detail, present tense>
SUMMARY: <2–3 sentences, 200–280 characters — sentence 1: the core action or data point; sentence 2: what it means for Nigeria's energy sector or economy>
ANALYSIS: <2 sentences — what this signals about NNPC's strategic direction or Nigeria's oil sector outlook>
---
<full HTML article body — do NOT repeat HEADLINE/SUMMARY/ANALYSIS as plain text>

ARTICLE STRUCTURE:
<h2> mirrors HEADLINE exactly
Opening <p>: the key NNPC action or data — what happened, the exact figure, and why it matters
<h3>Key Details</h3>: breakdown of the core facts — figures, partners, timelines, locations from the source
<h3>What This Means for Nigeria's Energy Sector</h3>: plain-English implications for fuel supply, the economy, investors
<h3>Context and Background</h3>: relevant sector context — use your knowledge, framed clearly as context
<h3>What Happens Next</h3>: next steps, implementation timeline, what to watch
Closing <p>: one sharp sentence on the broader significance for Nigeria's energy future

HTML: pure <h2>, <h3>, <p>, <strong>, <ul>, <li> — no html/body/head/style tags
Attribute to: NNPC Limited

IF REJECTING: SKIP: <one sentence reason>"""

    # ── Fetch posts from NNPC Strapi CMS ──────────────────────────────────────

    try:
        resp = _req.get(_API_URL, headers=_HEADERS, timeout=15, verify=False)
        resp.raise_for_status()
        api_data = resp.json()
    except Exception as exc:
        logger.error("NNPC scraper — API fetch failed: %s", exc)
        return "NNPC scraper: API fetch failed"

    posts = api_data.get('data', [])
    if not posts:
        return "NNPC scraper: no posts returned from API"

    created = 0

    for item in posts:
        if created >= MAX_PER_RUN:
            break

        title = (item.get('title') or '').strip()[:200]
        slug = (item.get('slug') or '').strip()
        content_html = item.get('content') or ''
        published_at = item.get('publishedAt') or item.get('post_date') or ''
        category = (item.get('post_category') or {}).get('category', 'Company News')

        if not title or not slug:
            continue

        # Date filter — only process articles from the last 2 days
        pub_date = _parse_date(published_at)
        if pub_date is not None and pub_date < cutoff:
            logger.debug("NNPC scraper — stale article skipped (%s): %s", pub_date.date(), title[:60])
            continue

        pub_date_str = pub_date.strftime("%d %B %Y") if pub_date else date_str
        dedup_url = f"{_ARTICLE_BASE}/{slug}"

        # URL dedup — skip if already covered in the last 30 days
        if Post.objects.filter(link=dedup_url, date_created__gte=thirty_days_ago).exists():
            logger.debug("NNPC scraper — already covered: %s", title[:60])
            continue

        body_text = _html_to_text(content_html)
        # Skip PDF-only posts (no real content)
        if len(body_text) < 100:
            logger.debug("NNPC scraper — content too thin, skipping: %s", title[:60])
            continue

        prompt = build_nnpc_prompt(title, body_text, pub_date_str, category)
        raw = call_llm(prompt)
        if not raw:
            continue

        if raw.strip().upper().startswith('SKIP:'):
            logger.info("NNPC scraper — LLM rejected '%s': %s", title[:60], raw[5:].strip()[:100])
            continue

        # Extract SEO headline
        seo_title = title
        headline_match = _re.search(r'^HEADLINE:\s*(.+)', raw, _re.MULTILINE | _re.IGNORECASE)
        if headline_match:
            candidate = headline_match.group(1).strip()[:200]
            if len(candidate) > 15:
                seo_title = candidate
            raw = _re.sub(r'^HEADLINE:.*\n?', '', raw, flags=_re.MULTILINE | _re.IGNORECASE).strip()

        summary, analysis, html_body = parse_llm_response(raw)
        if not html_body or len(html_body.strip()) < 50:
            logger.warning("NNPC scraper — empty body for: %s", title[:80])
            continue

        if not summary:
            summary = seo_title[:160]

        try:
            post = Post.objects.create(
                title=seo_title,
                body=html_body,
                summary=summary,
                analysis=analysis or None,
                link=dedup_url,
                source='scrape',
                source_domain='nnpcgroup.com',
                is_published=False,
                updated_by='nnpc-auto',
                author=default_author,
                tags='nnpc, oil and gas, nigeria, energy, petroleum, nnpc limited',
            )
            if energy_cat:
                post.categories.add(energy_cat)
            created += 1
            logger.info("NNPC scraper — created draft: %s", seo_title[:80])
        except Exception as exc:
            logger.error("NNPC scraper — save failed [%s]: %s", seo_title[:80], exc)

        _time.sleep(2)

    return f"NNPC scraper: {created} new article(s) created"


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def scrape_nbs_data(self):
    """
    Scraper for NBS (National Bureau of Statistics) statistical data releases.
    Uses the NBS microdata catalog API for newly added datasets and the
    elibrary for their associated summary text.
    Runs 4x/day. Deduplicates by catalog URL. Saves unpublished drafts.
    """
    import re as _re
    import time as _time
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    import requests as _req
    from bs4 import BeautifulSoup as _BS
    from django.conf import settings
    from django.utils import timezone
    from .models import Post, Category, Author
    from .data_journalism import parse_llm_response

    api_key = getattr(settings, 'GROQ_API_KEY', '')
    if not api_key:
        return "GROQ_API_KEY not configured"

    try:
        from groq import Groq
    except ImportError:
        return "groq package missing"

    _CATALOG_HISTORY = 'https://microdata.nigerianstat.gov.ng/index.php/catalog/history'
    _CATALOG_BASE = 'https://microdata.nigerianstat.gov.ng/index.php/catalog'
    _HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; PulseLineDaily-GovBot/1.0)', 'Accept': 'text/html, application/json, */*'}
    MAX_PER_RUN = 4

    client = Groq(api_key=api_key)
    now = timezone.now()
    date_str = now.strftime("%d %B %Y")
    ninety_days_ago = now - _td(days=90)

    articles_cat = (
        Category.objects.filter(name__icontains='article').first()
        or Category.objects.filter(name__icontains='econom').first()
        or Category.objects.first()
    )
    default_author = Author.objects.filter(slug='clinton-nwachukwu').first()

    def _fetch_catalog_details(catalog_id):
        """Fetch title, date (from JSON-LD), and any available abstract from the catalog page."""
        import json as _json
        url = f'{_CATALOG_BASE}/{catalog_id}'
        try:
            with _warnings.catch_warnings():
                _warnings.simplefilter('ignore')
                r = _req.get(url, headers=_HEADERS, timeout=12, verify=False)
            r.raise_for_status()
            soup = _BS(r.text, 'lxml')

            # Title — JSON-LD is most reliable; strip the " - Nigeria" suffix
            page_title = ''
            ld_tag = soup.find('script', type='application/ld+json')
            ld_data = {}
            if ld_tag:
                try:
                    ld_data = _json.loads(ld_tag.string or '{}')
                except Exception:
                    pass
            raw_name = ld_data.get('name', '')
            if raw_name:
                page_title = _re.sub(r'\s*-\s*Nigeria\s*$', '', raw_name, flags=_re.IGNORECASE).strip()
            if not page_title:
                h1 = soup.find('h1')
                if h1:
                    page_title = h1.get_text(strip=True)

            # Date — prefer JSON-LD dateCreated (ISO 8601 with timezone)
            pub_date = None
            date_created = ld_data.get('dateCreated', '')
            if date_created:
                try:
                    pub_date = _dt.fromisoformat(date_created).replace(tzinfo=_tz.utc)
                except Exception:
                    try:
                        pub_date = _dt.strptime(date_created[:10], '%Y-%m-%d').replace(tzinfo=_tz.utc)
                    except Exception:
                        pass

            # Abstract — JSON-LD description if available, otherwise empty
            abstract_text = (ld_data.get('description') or '').strip()
            if not abstract_text:
                abstract_el = soup.find(class_='abstract')
                if abstract_el:
                    abstract_text = abstract_el.get_text(separator=' ', strip=True)

            return page_title, pub_date, abstract_text[:3000]
        except Exception as exc:
            logger.warning("NBS scraper — failed to fetch catalog/%s: %s", catalog_id, exc)
            return '', None, ''

    def call_llm(prompt):
        try:
            resp = client.chat.completions.create(
                model='llama-3.3-70b-versatile',
                max_tokens=2500,
                messages=[{'role': 'user', 'content': prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.error("Groq API error (NBS scraper): %s", exc)
            return None

    def build_nbs_prompt(title, abstract_text, pub_date_str, sector):
        has_data = len(abstract_text.strip()) > 80
        source_block = f"DATASET DESCRIPTION:\n{abstract_text[:2800]}" if has_data else "NOTE: No abstract available — write based on the dataset title and your knowledge of Nigerian statistics."
        return f"""You are a senior Nigerian data journalist at PulseLineDaily, Nigeria's leading digital news outlet published on Google News and monetised via AdSense.

TODAY'S DATE: {date_str}
DATASET RELEASE DATE: {pub_date_str}
SOURCE: National Bureau of Statistics (NBS), Nigeria
SECTOR: {sector}
DATASET TITLE: {title}

{source_block}

EDITORIAL GATE — respond only with SKIP: <reason> if:
- The dataset is a historical archive with no current news relevance (pre-2020 survey with no new data)
- The content is too thin to write a factual, informative article without inventing statistics
- It is a purely technical/methodological document with no public interest angle

WRITE if the dataset covers: inflation/CPI, GDP, labour force, trade statistics, poverty, FAAC disbursements, price watches, demographic data, sector reports (electricity, transport, agriculture), or any new NBS statistical release with clear public interest.

STRICT FACTUAL RULES:
- Do NOT fabricate specific figures, percentages, or numbers not in the dataset description
- If specific figures are available in the description, use them — they are authoritative NBS data
- If no specific figures are provided, explain what the dataset measures and why it matters
- You MAY add context from your knowledge of Nigeria's economy and statistics

WRITING QUALITY — data journalism must make numbers meaningful:
- Open with the most important finding or what this data release tells Nigerians
- Translate numbers into plain English: "inflation rose to 32%" → "prices are rising 32% faster than last year"
- Short paragraphs (2–3 sentences), active voice
- Specific beats vague: "food prices rose 41%" beats "food prices increased significantly"
- BANNED openers: "In a significant development", "It is noteworthy", "Against the backdrop of", "Furthermore"
- Length: 500–650 words

SEO HEADLINE RULES (HEADLINE: field):
- 55–65 characters
- Format: [NBS data finding or release] [key figure or topic] — present tense
- Good: "Nigeria Inflation Hits 32% as Food Prices Surge" | "NBS: GDP Grows 3.4% in Q3 2024"
- Bad: "NBS Releases New Data on Inflation" | "Important Statistics Released by NBS"
- BANNED: "Significant", "Major", "Key", "Important", "New Report", "Update"

OUTPUT FORMAT (exactly as shown):
HEADLINE: <55–65 char SEO title — the key finding or data point, present tense>
SUMMARY: <2–3 sentences, 200–280 characters — sentence 1: the headline finding; sentence 2: what it means for ordinary Nigerians>
ANALYSIS: <2 sentences — what this data signals about Nigeria's economic trajectory and what to watch>
---
<full HTML article body — do NOT repeat HEADLINE/SUMMARY/ANALYSIS as plain text>

ARTICLE STRUCTURE:
<h2> mirrors HEADLINE exactly
Opening <p>: the headline data point and its immediate significance for Nigeria
<h3>What the Data Shows</h3>: key findings — use specific figures from the description where available
<h3>What It Means for Nigerians</h3>: plain-English impact on households, businesses, workers, or the economy
<h3>Behind the Numbers</h3>: methodology context, how this data is collected, what it measures — use your knowledge
<h3>What to Watch</h3>: key trends, next release date if known, what analysts and policymakers are watching
Closing <p>: one sharp sentence on the broader economic significance

HTML: pure <h2>, <h3>, <p>, <strong>, <ul>, <li> — no html/body/head/style tags
Attribute to: National Bureau of Statistics (NBS)

IF REJECTING: SKIP: <one sentence reason>"""

    # ── Fetch catalog entry list from history page (HTML, sorted by ID desc) ──

    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter('ignore')
        try:
            hist_resp = _req.get(_CATALOG_HISTORY, headers=_HEADERS, timeout=15, verify=False)
            hist_resp.raise_for_status()
        except Exception as exc:
            logger.error("NBS scraper — catalog history page unreachable: %s", exc)
            return "NBS scraper: catalog history unreachable"

    hist_soup = _BS(hist_resp.text, 'lxml')

    # Collect all /catalog/{id} links — ID is the proxy for recency (higher = newer)
    seen_ids: set = set()
    entries_raw = []
    for a in hist_soup.find_all('a', href=True):
        m = _re.search(r'/catalog/(\d+)', a['href'])
        if m:
            cat_id = int(m.group(1))
            if cat_id not in seen_ids:
                seen_ids.add(cat_id)
                title_text = a.get_text(strip=True)
                entries_raw.append((cat_id, title_text))

    if not entries_raw:
        return "NBS scraper: no catalog entries found on history page"

    # Highest ID = most recently added
    entries_raw.sort(key=lambda x: x[0], reverse=True)
    # Only look at the 40 most recently added entries — older ones are already in DB
    candidate_entries = entries_raw[:40]
    logger.info("NBS scraper — found %d catalog IDs (top 40 evaluated)", len(entries_raw))

    created = 0

    for cat_id, hint_title in candidate_entries:
        if created >= MAX_PER_RUN:
            break

        dedup_url = f"{_CATALOG_BASE}/{cat_id}"

        # URL dedup — skip if already in DB in the last 90 days
        if Post.objects.filter(link=dedup_url, date_created__gte=ninety_days_ago).exists():
            logger.debug("NBS scraper — already covered: %s", dedup_url)
            continue

        # Fetch full details from the catalog entry page
        page_title, pub_date, abstract_text = _fetch_catalog_details(cat_id)
        _time.sleep(1)

        # Date freshness gate — skip datasets added to the catalog more than 90 days ago
        if pub_date is not None and pub_date < ninety_days_ago:
            logger.debug("NBS scraper — stale catalog entry skipped (%s): %s", pub_date.date(), (page_title or hint_title)[:60])
            continue

        title = (page_title or hint_title or '').strip()[:200]
        if not title:
            continue

        sector = 'statistics'
        pub_date_str = pub_date.strftime("%d %B %Y") if pub_date else date_str

        prompt = build_nbs_prompt(title, abstract_text, pub_date_str, sector)
        raw = call_llm(prompt)
        if not raw:
            continue

        if raw.strip().upper().startswith('SKIP:'):
            logger.info("NBS scraper — LLM rejected '%s': %s", title[:60], raw[5:].strip()[:100])
            continue

        # Extract SEO headline
        seo_title = title
        headline_match = _re.search(r'^HEADLINE:\s*(.+)', raw, _re.MULTILINE | _re.IGNORECASE)
        if headline_match:
            candidate = headline_match.group(1).strip()[:200]
            if len(candidate) > 15:
                seo_title = candidate
            raw = _re.sub(r'^HEADLINE:.*\n?', '', raw, flags=_re.MULTILINE | _re.IGNORECASE).strip()

        summary, analysis, html_body = parse_llm_response(raw)
        if not html_body or len(html_body.strip()) < 50:
            logger.warning("NBS scraper — empty body for: %s", title[:80])
            continue

        if not summary:
            summary = seo_title[:160]

        try:
            post = Post.objects.create(
                title=seo_title,
                body=html_body,
                summary=summary,
                analysis=analysis or None,
                link=dedup_url,
                source='scrape',
                source_domain='nigerianstat.gov.ng',
                is_published=False,
                updated_by='nbs-auto',
                author=default_author,
                tags='nbs, statistics, nigeria, data, economy, national bureau of statistics',
            )
            if articles_cat:
                post.categories.add(articles_cat)
            created += 1
            logger.info("NBS scraper — created draft: %s", seo_title[:80])
        except Exception as exc:
            logger.error("NBS scraper — save failed [%s]: %s", seo_title[:80], exc)
            continue

        _time.sleep(2)

    return f"NBS scraper: {created} new article(s) created"


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def scrape_ndlea_news(self):
    """
    Scraper for NDLEA (National Drug Law Enforcement Agency) news.
    Fetches from the HTML listing page, parses article cards, fetches
    full article bodies and rewrites via Groq LLM.
    Runs 4x/day. Deduplicates by URL. Saves unpublished drafts.
    """
    import re as _re
    import time as _time
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    import requests as _req
    from bs4 import BeautifulSoup as _BS
    from django.conf import settings
    from django.utils import timezone
    from .models import Post, Category, Author
    from .data_journalism import parse_llm_response

    api_key = getattr(settings, 'GROQ_API_KEY', '')
    if not api_key:
        return "GROQ_API_KEY not configured"

    try:
        from groq import Groq
    except ImportError:
        return "groq package missing"

    _NEWS_URL = 'https://www.ndlea.gov.ng/news/'
    _HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; PulseLineDaily-GovBot/1.0)', 'Accept': 'text/html,*/*'}
    DAYS_BACK = 2
    MAX_PER_RUN = 6

    client = Groq(api_key=api_key)
    now = timezone.now()
    date_str = now.strftime("%d %B %Y")
    cutoff = (_dt.now(_tz.utc) - _td(days=DAYS_BACK)).replace(hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago = now - _td(days=30)

    politics_cat = (
        Category.objects.filter(name__icontains='polit').first()
        or Category.objects.filter(name__icontains='article').first()
        or Category.objects.first()
    )
    default_author = Author.objects.filter(slug='clinton-nwachukwu').first()

    def _parse_date(s):
        # Strip time component: "Jul 5, 2026 - 10:39" → "Jul 5, 2026"
        s = _re.sub(r'\s*[–—-]\s*\d{1,2}:\d{2}.*', '', s).strip()
        for fmt in ('%b %d, %Y', '%B %d, %Y'):
            try:
                return _dt.strptime(s, fmt).replace(tzinfo=_tz.utc)
            except Exception:
                pass
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
            logger.error("Groq API error (NDLEA scraper): %s", exc)
            return None

    def build_ndlea_prompt(title, subtitle, body_text, pub_date_str):
        source_block = ''
        if subtitle:
            source_block += f"SUBHEADLINE: {subtitle}\n\n"
        if body_text:
            source_block += f"ORIGINAL ARTICLE:\n{body_text[:3000]}"
        else:
            source_block = "NOTE: No article body — write based on the headline and your knowledge of NDLEA operations."
        return f"""You are a senior Nigerian crime and law enforcement journalist at PulseLineDaily, Nigeria's leading digital news outlet published on Google News and monetised via AdSense.

TODAY'S DATE: {date_str}
PUBLICATION DATE: {pub_date_str}
SOURCE: National Drug Law Enforcement Agency (NDLEA), Nigeria
ARTICLE HEADLINE: {title}

{source_block}

EDITORIAL GATE — respond only with SKIP: <reason> if:
- The article is a recruitment notice, procurement notice, administrative circular, or gallery entry
- It is a press statement that is clearly a public notice with no news value
- The content is too thin to write a meaningful article (< 3 paragraphs worth of facts)

WRITE if the article covers: drug seizures, drug lab busts, arrests, convictions, international operations, asset forfeitures, policy announcements, or any law enforcement action with public interest.

STRICT FACTUAL RULES:
- Do NOT fabricate quantities, names, nationalities, or locations not in the source
- Use specific figures from the source (kg, street value, number of suspects)
- Do NOT write "the article says" or "according to the source" — write as a reporter who was briefed

WRITING QUALITY — crime reporting must be precise and compelling:
- Lead with the most dramatic and specific fact: quantity seized, nationality of suspect, location of bust
- Short punchy paragraphs (2–3 sentences), active voice
- Translate numbers: "€2m" → "about ₦3.5 billion"
- Use the names and ranks of NDLEA officials from the source
- BANNED openers: "In a significant development", "It is noteworthy", "Against the backdrop of", "The NDLEA has"
- Length: 450–600 words

SEO HEADLINE RULES (HEADLINE: field):
- 55–65 characters
- Format: [specific action — who/what/where/how much], present tense
- Good: "NDLEA Seizes 6,778kg Canadian Loud at Lagos Port" | "Mexican Arrested as NDLEA Busts Oyo Meth Lab"
- Bad: "NDLEA Makes Another Arrest" | "NDLEA Reports Drug Seizure"
- BANNED: "Significant", "Major", "Key", "Important", "Landmark", "Historic"

OUTPUT FORMAT (exactly as shown):
HEADLINE: <55–65 char SEO title>
SUMMARY: <2–3 sentences, 200–280 characters — sentence 1: the specific operation/seizure; sentence 2: NDLEA response or significance>
ANALYSIS: <2 sentences — what this tells us about Nigeria's drug trafficking landscape and what to watch>
---
<full HTML article body — do NOT repeat HEADLINE/SUMMARY/ANALYSIS as plain text>

ARTICLE STRUCTURE:
<h2> mirrors HEADLINE exactly
Opening <p>: the most specific, compelling fact from the operation
<h3>The Operation</h3>: what happened, where, who was involved — use all specific details from source
<h3>What Was Seized</h3>: quantities, substances, street value if given, packaging details
<h3>NDLEA's Response</h3>: Marwa quote (if in source) or official NDLEA statement
<h3>The Bigger Picture</h3>: connection to broader drug trafficking trends in Nigeria/West Africa
Closing <p>: one sharp sentence on what this signals for Nigeria's anti-narcotics campaign

HTML: pure <h2>, <h3>, <p>, <strong>, <ul>, <li> — no html/body/head/style tags
Attribute all facts to: NDLEA, National Drug Law Enforcement Agency

IF REJECTING: SKIP: <one sentence reason>"""

    # ── Fetch listing page ────────────────────────────────────────────────────
    try:
        resp = _req.get(_NEWS_URL, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("NDLEA scraper — listing page unreachable: %s", exc)
        return "NDLEA scraper: listing page unreachable"

    soup = _BS(resp.text, 'lxml')
    cards = soup.find_all(class_='featured-slider-item')
    if not cards:
        return "NDLEA scraper: no article cards found on listing page"

    logger.info("NDLEA scraper — found %d article cards", len(cards))
    created = 0

    for card in cards:
        if created >= MAX_PER_RUN:
            break

        # Extract article link (img-link) and caption
        img_link = card.find('a', class_='img-link')
        if not img_link:
            continue
        article_url = img_link.get('href', '').strip()
        if not article_url or '/news/' not in article_url:
            continue

        cap = card.find(class_='caption')
        if not cap:
            continue

        title_el = cap.find(class_='title')
        title = title_el.get_text(strip=True)[:200] if title_el else ''
        if not title:
            continue

        meta = cap.find(class_='post-meta')
        date_raw = meta.find('span').get_text(strip=True) if meta and meta.find('span') else ''
        pub_date = _parse_date(date_raw)

        # Freshness filter
        if pub_date is not None and pub_date < cutoff:
            logger.debug("NDLEA scraper — stale article skipped (%s): %s", pub_date.date(), title[:60])
            continue

        # URL dedup
        if Post.objects.filter(link=article_url, date_created__gte=thirty_days_ago).exists():
            logger.debug("NDLEA scraper — already covered: %s", title[:60])
            continue

        # Fetch full article page
        try:
            art_resp = _req.get(article_url, headers=_HEADERS, timeout=15)
            art_resp.raise_for_status()
        except Exception as exc:
            logger.warning("NDLEA scraper — article fetch failed [%s]: %s", article_url[-50:], exc)
            continue

        art_soup = _BS(art_resp.text, 'lxml')

        # Use article page date if more precise (has time component)
        first_meta = art_soup.find(class_='post-meta')
        if first_meta:
            art_date_raw = first_meta.find('span').get_text(strip=True) if first_meta.find('span') else ''
            art_date = _parse_date(art_date_raw)
            if art_date:
                pub_date = art_date

        pub_date_str = pub_date.strftime("%d %B %Y") if pub_date else date_str

        # Article body
        body_el = art_soup.find(class_='post-text')
        body_text = body_el.get_text(separator='\n', strip=True)[:3000] if body_el else ''

        if len(body_text) < 100:
            logger.debug("NDLEA scraper — article body too short, skipping: %s", title[:60])
            continue

        # Subtitle from post-summary
        summary_el = art_soup.find(class_='post-summary')
        subtitle = ''
        if summary_el:
            sub_h2 = summary_el.find('h2')
            if sub_h2:
                subtitle = sub_h2.get_text(strip=True)[:300]

        _time.sleep(1)

        prompt = build_ndlea_prompt(title, subtitle, body_text, pub_date_str)
        raw = call_llm(prompt)
        if not raw:
            continue

        if raw.strip().upper().startswith('SKIP:'):
            logger.info("NDLEA scraper — LLM rejected '%s': %s", title[:60], raw[5:].strip()[:100])
            continue

        # Extract SEO headline
        seo_title = title
        headline_match = _re.search(r'^HEADLINE:\s*(.+)', raw, _re.MULTILINE | _re.IGNORECASE)
        if headline_match:
            candidate = headline_match.group(1).strip()[:200]
            if len(candidate) > 15:
                seo_title = candidate
            raw = _re.sub(r'^HEADLINE:.*\n?', '', raw, flags=_re.MULTILINE | _re.IGNORECASE).strip()

        summary, analysis, html_body = parse_llm_response(raw)
        if not html_body or len(html_body.strip()) < 50:
            logger.warning("NDLEA scraper — empty body for: %s", title[:80])
            continue

        if not summary:
            summary = seo_title[:160]

        try:
            post = Post.objects.create(
                title=seo_title,
                body=html_body,
                summary=summary,
                analysis=analysis or None,
                link=article_url,
                source='scrape',
                source_domain='ndlea.gov.ng',
                is_published=False,
                updated_by='ndlea-auto',
                author=default_author,
                tags='ndlea, drugs, law enforcement, nigeria, anti-narcotics, drug trafficking',
            )
            if politics_cat:
                post.categories.add(politics_cat)
            created += 1
            logger.info("NDLEA scraper — created draft: %s", seo_title[:80])
        except Exception as exc:
            logger.error("NDLEA scraper — save failed [%s]: %s", seo_title[:80], exc)
            continue

        _time.sleep(2)

    return f"NDLEA scraper: {created} new article(s) created"
