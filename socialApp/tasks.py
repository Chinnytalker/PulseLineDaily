import logging

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


def _summary_text(post, max_chars=350):
    """Return the post summary for social sharing, truncated if needed."""
    text = (post.summary or '').strip()
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_period = truncated.rfind('. ')
    if last_period > max_chars // 2:
        return truncated[:last_period + 1]
    return truncated.rstrip() + '...'


# ── Facebook ──────────────────────────────────────────────────────────────────

def _share_to_facebook(post):
    from .models import SocialPostLog

    token = getattr(settings, 'FACEBOOK_PAGE_ACCESS_TOKEN', '')
    page_id = getattr(settings, 'FACEBOOK_PAGE_ID', '')

    if not token or not page_id:
        logger.warning("Facebook: credentials not configured — skipping post '%s'", post.title[:50])
        return

    summary = _summary_text(post)
    post_url = _post_full_url(post)
    message = f"{post.title}\n\n{summary}\n\n#PulseLineDaily #NigeriaNews"

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

    post_url = _post_full_url(post)
    safe_title = post.title.replace('<', '&lt;').replace('>', '&gt;')

    # Try to get the Cloudinary image URL directly
    image_url = None
    try:
        if post.image:
            image_url = post.image.url
    except Exception:
        image_url = None

    safe_summary = _summary_text(post).replace('<', '&lt;').replace('>', '&gt;')

    try:
        if image_url:
            # sendPhoto: attach article image directly — no more site logo previews
            # Caption max is 1024 chars
            caption = (
                f"<b>{safe_title}</b>\n\n"
                f"{safe_summary}\n\n"
                f'<a href="{post_url}">Read the full story</a>\n\n'
                f"#PulseLineDaily #NigeriaNews"
            )
            resp = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendPhoto",
                json={
                    'chat_id': channel_id,
                    'photo': image_url,
                    'caption': caption,
                    'parse_mode': 'HTML',
                },
                timeout=15,
            )
        else:
            # No image — fall back to text message with link preview
            text = (
                f"<b>{safe_title}</b>\n\n"
                f"{safe_summary}\n\n"
                f'<a href="{post_url}">Read the full story</a>\n\n'
                f"#PulseLineDaily #NigeriaNews"
            )
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


# ── OneSignal Push Notifications ─────────────────────────────────────────────

def _send_push_notification(post):
    from .models import SocialPostLog

    app_id = getattr(settings, 'ONESIGNAL_APP_ID', '')
    api_key = getattr(settings, 'ONESIGNAL_REST_API_KEY', '')

    if not app_id or not api_key:
        logger.warning("OneSignal: credentials not configured — skipping post '%s'", post.title[:50])
        return

    post_url = _post_full_url(post)
    summary = _summary_text(post, max_chars=200)

    try:
        resp = requests.post(
            'https://onesignal.com/api/v1/notifications',
            headers={
                'Authorization': f'Basic {api_key}',
                'Content-Type': 'application/json',
            },
            json={
                'app_id': app_id,
                'included_segments': ['All'],
                'headings': {'en': post.title[:100]},
                'contents': {'en': summary or post.title[:200]},
                'url': post_url,
            },
            timeout=15,
        )
        data = resp.json()

        if data.get('id'):
            SocialPostLog.objects.create(
                post=post, platform='onesignal',
                success=True, platform_post_id=data['id'],
            )
            logger.info("OneSignal: push sent '%s' → %s", post.title[:50], data['id'])
        else:
            error = str(data.get('errors', data))
            SocialPostLog.objects.create(
                post=post, platform='onesignal',
                success=False, error_message=error,
            )
            logger.warning("OneSignal: push failed for '%s': %s", post.title[:50], error)

    except Exception as exc:
        SocialPostLog.objects.create(
            post=post, platform='onesignal',
            success=False, error_message=str(exc),
        )
        logger.error("OneSignal: exception for '%s': %s", post.title[:50], exc)


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
    already_push = set(
        SocialPostLog.objects.filter(
            platform='onesignal', success=True, post__in=candidates,
        ).values_list('post_id', flat=True)
    )

    pending_fb = list(candidates.exclude(pk__in=already_fb).order_by('date_created')[:MAX_PER_RUN])
    pending_tg = list(candidates.exclude(pk__in=already_tg).order_by('date_created')[:MAX_PER_RUN])
    pending_push = list(candidates.exclude(pk__in=already_push).order_by('date_created')[:MAX_PER_RUN])

    for post in pending_fb:
        _share_to_facebook(post)

    for post in pending_tg:
        _share_to_telegram(post)

    for post in pending_push:
        _send_push_notification(post)

    logger.info(
        "Social poster run complete — Facebook: %d, Telegram: %d, Push: %d",
        len(pending_fb), len(pending_tg), len(pending_push),
    )
