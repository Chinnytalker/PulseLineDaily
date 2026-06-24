import re
from django import template
from django.utils.html import linebreaks
from django.utils.safestring import mark_safe

register = template.Library()


@register.inclusion_tag('blog/partials/breaking_ticker.html')
def breaking_news_ticker():
    from django.utils import timezone
    from projectApp.models import Post
    posts = list(
        Post.published.filter(
            is_breaking=True,
            breaking_expiry__gte=timezone.now()
        ).order_by('-date_created')[:8]
    )
    return {'breaking_posts': posts}


@register.filter(is_safe=True)
def render_body(value):
    """
    Render a post body correctly whether it was written as plain text or HTML.

    - Content that contains HTML tags (written with CKEditor) → rendered with |safe
    - Plain-text content (written before the editor was added) → converted with linebreaks
      so newlines become <br> / <p> tags just as before
    """
    if not value:
        return ''
    if re.search(r'<[a-zA-Z][^>]*>', value):
        return mark_safe(value)
    return mark_safe(linebreaks(value))
