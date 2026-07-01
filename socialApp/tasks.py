import logging
import re

import requests
from celery import shared_task
from django.conf import settings
from django.utils import timezone
from datetime import timedelta

logger = logging.getLogger(__name__)

MAX_PER_RUN = 3
SHARE_WINDOW_DAYS = 7


def _site_url():
    return getattr(settings, 'SITE_URL', 'https://www.pulselinedaily.com').rstrip('/')


def _post_full_url(post):
    return f"{_site_url()}{post.get_absolute_url()}"


def _plain_excerpt(post, max_chars=280):
    """Return a plain-text excerpt from summary or body (strips HTML tags)."""
    text = post.summary or ''
    if not text and post.body:
        text = re.sub(r'<[^>]+>', ' ', post.body)
        text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_chars].rstrip()


# ── Facebook ──────────────────────────────────────────────────────────────────

def _share_to_facebook(post):
    from .models import SocialPostLog

    token = getattr(settings, 'FACEBOOK_PAGE_ACCESS_TOKEN', '')
    page_id = getattr(settings, 'FACEBOOK_PAGE_ID', '')

    if not token or not page_id:
        logger.warning("Facebook: credentials not configured — skipping post '%s'", post.title[:50])
        return

    excerpt = _plain_excerpt(post)
    post_url = _post_full_url(post)
    message = f"{post.title}\n\n{excerpt}\n\n#PulseLineDaily #NigeriaNews"

    try:
        resp = requests.post(
            f"https://graph.facebook.com/v19.0/{page_id}/feed",
            data={'message': message, 'link': post_url, 'access_token': token},
            timeout=15,
        )
        data = resp.json()

        if 'id' in data:
            SocialPostLog.objects.create(
                post=post, platform='facebook',
                success=True, platform_post_id=data['id'],
            )
            logger.info("Facebook: shared '%s' → %s", post.title[:50], data['id'])
        else:
            error = data.get('error', {}).get('message', str(data))
            SocialPostLog.objects.create(
                post=post, platform='facebook',
                success=False, error_message=error,
            )
            logger.warning("Facebook: share failed for '%s': %s", post.title[:50], error)

    except Exception as exc:
        SocialPostLog.objects.create(
            post=post, platform='facebook',
            success=False, error_message=str(exc),
        )
        logger.error("Facebook: exception for '%s': %s", post.title[:50], exc)


# ── Telegram ──────────────────────────────────────────────────────────────────

def _share_to_telegram(post):
    from .models import SocialPostLog

    bot_token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
    channel_id = getattr(settings, 'TELEGRAM_CHANNEL_ID', '')

    if not bot_token or not channel_id:
        logger.warning("Telegram: credentials not configured — skipping post '%s'", post.title[:50])
        return

    excerpt = _plain_excerpt(post, max_chars=300)
    post_url = _post_full_url(post)

    # Escape angle brackets in title/excerpt so Telegram HTML parser doesn't choke
    safe_title = post.title.replace('<', '&lt;').replace('>', '&gt;')
    safe_excerpt = excerpt.replace('<', '&lt;').replace('>', '&gt;')

    text = (
        f"📰 <b>{safe_title}</b>\n\n"
        f"{safe_excerpt}\n\n"
        f'<a href="{post_url}">Read the full story →</a>\n\n'
        f"#PulseLineDaily #NigeriaNews"
    )

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                'chat_id': channel_id,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': False,
            },
            timeout=15,
        )
        data = resp.json()

        if data.get('ok'):
            msg_id = str(data['result']['message_id'])
            SocialPostLog.objects.create(
                post=post, platform='telegram',
                success=True, platform_post_id=msg_id,
            )
            logger.info("Telegram: shared '%s' → msg_id %s", post.title[:50], msg_id)
        else:
            error = data.get('description', str(data))
            SocialPostLog.objects.create(
                post=post, platform='telegram',
                success=False, error_message=error,
            )
            logger.warning("Telegram: share failed for '%s': %s", post.title[:50], error)

    except Exception as exc:
        SocialPostLog.objects.create(
            post=post, platform='telegram',
            success=False, error_message=str(exc),
        )
        logger.error("Telegram: exception for '%s': %s", post.title[:50], exc)


# ── Main periodic task ────────────────────────────────────────────────────────

@shared_task(name='socialApp.tasks.share_published_posts')
def share_published_posts():
    """
    Find published posts from the last 7 days that haven't been shared yet
    and post them to Facebook and Telegram. Runs every 15 minutes via Celery Beat.
    """
    from projectApp.models import Post
    from .models import SocialPostLog

    window_start = timezone.now() - timedelta(days=SHARE_WINDOW_DAYS)
    candidates = Post.published.filter(date_created__gte=window_start)

    already_fb = set(
        SocialPostLog.objects.filter(
            platform='facebook', success=True, post__in=candidates,
        ).values_list('post_id', flat=True)
    )
    already_tg = set(
        SocialPostLog.objects.filter(
            platform='telegram', success=True, post__in=candidates,
        ).values_list('post_id', flat=True)
    )

    pending_fb = list(candidates.exclude(pk__in=already_fb).order_by('date_created')[:MAX_PER_RUN])
    pending_tg = list(candidates.exclude(pk__in=already_tg).order_by('date_created')[:MAX_PER_RUN])

    for post in pending_fb:
        _share_to_facebook(post)

    for post in pending_tg:
        _share_to_telegram(post)

    logger.info(
        "Social poster run complete — Facebook: %d, Telegram: %d",
        len(pending_fb), len(pending_tg),
    )
