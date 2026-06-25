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
                max_tokens=2200,
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
    from .data_journalism import fetch_rss_stories, build_rewrite_prompt, detect_story_category

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
                max_tokens=2200,
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

    # ── Fetch & rewrite ───────────────────────────────────────────────────────

    stories = fetch_rss_stories(max_per_source=2)

    for story in stories:
        if created >= MAX_PER_RUN:
            break

        if already_exists(story["link"]):
            logger.debug("Already covered: %s", story["link"])
            continue

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

        post = Post.objects.create(
            title=title,
            body=html,
            summary=summary,
            analysis=analysis or None,
            tags=f"nigeria, {story['category'].lower()}, breaking news, analysis",
            link=story["link"],
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
        build_epl_standings_prompt,
        build_nigeria_results_prompt,
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
                max_tokens=2200,
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

    # ── 1. EPL standings (once per 3 days) ───────────────────────────────────
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

    # ── 2. Nigeria Super Eagles results (once per 4 days) ─────────────────────
    if not recently_generated("super eagles", days=4):
        data = fetch_nigeria_results()
        if data and data.get("results"):
            raw = call_llm(build_nigeria_results_prompt(data))
            if raw:
                summary, analysis, html = parse_response(raw)
                save_draft(
                    title=f"Super Eagles Form Guide: Recent Results Analysed — {now.strftime('%B %Y')}",
                    body=html,
                    summary=summary,
                    analysis=analysis,
                    tags="super eagles, nigeria, football, world cup 2026, caf, sports",
                )
                created += 1

    # ── 3. Static sports analysis topics (rotate by day of month) ────────────
    topic_index = now.day % len(SPORTS_ANALYSIS_TOPICS)
    topic = SPORTS_ANALYSIS_TOPICS[topic_index]

    if not recently_generated(topic["key"], days=topic["frequency_days"]):
        raw = call_llm(build_static_sports_prompt(topic))
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
        build_brent_prompt,
        build_ngx_prompt,
        build_commodity_prompt,
        build_acled_prompt,
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
                max_tokens=2200,
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

    return f"Market articles task complete: {created} draft(s) created"
