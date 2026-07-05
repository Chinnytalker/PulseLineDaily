from django.core.cache import cache
from django.db.models.signals import post_save
from django.dispatch import receiver


HOMEPAGE_KEYS = ['hp_trending', 'hp_categories', 'hp_breaking', 'all_categories_page']


@receiver(post_save, sender='projectApp.Post')
def clear_homepage_cache(sender, instance, **kwargs):
    if instance.is_published:
        cache.delete_many(HOMEPAGE_KEYS)
        # Purge Cloudflare edge cache so the published article is visible immediately
        try:
            from .tasks import purge_cloudflare_cache
            cats = list(instance.categories.values_list('name', flat=True))
            purge_cloudflare_cache.delay(slug=instance.slug, category_names=cats)
        except Exception:
            pass  # never let a cache purge failure prevent saving
