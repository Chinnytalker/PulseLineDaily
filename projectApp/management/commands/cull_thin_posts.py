from django.core.management.base import BaseCommand
from django.db.models import Q

from projectApp.models import Post

# updated_by markers written by the paused generators (see tasks.py)
REWRITE_MARKER = "rss-auto"
STATIC_TOPIC_MARKERS = ("politics-auto", "entertainment-auto")

# sports-auto also produces data-driven pieces we want to KEEP, so static
# sports posts are matched by the topic keys their generator puts in tags
STATIC_SPORTS_KEYS = (
    "africa_world_cup_2026_campaign",
    "nigeria_world_cup_history",
    "world_cup_2026_global_spotlight",
)


class Command(BaseCommand):
    help = (
        "Unpublish thin auto-generated posts (RSS rewrites and static-topic "
        "articles) — AdSense 'Low value content' remediation. Posts become "
        "drafts, nothing is deleted. Dry run by default; pass --apply to "
        "actually unpublish."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Actually unpublish the matched posts (default is a dry run)',
        )
        parser.add_argument(
            '--keep-rewrites', action='store_true',
            help='Leave RSS rewrite posts published; only cull static-topic posts',
        )

    def handle(self, *args, **options):
        static_sports_q = Q()
        for key in STATIC_SPORTS_KEYS:
            static_sports_q |= Q(tags__icontains=key)

        target_q = (
            Q(updated_by__in=STATIC_TOPIC_MARKERS)
            | (Q(updated_by="sports-auto") & static_sports_q)
        )
        if not options['keep_rewrites']:
            target_q |= Q(updated_by=REWRITE_MARKER)

        qs = Post.objects.filter(is_published=True).filter(target_q)

        total = qs.count()
        if not total:
            self.stdout.write(self.style.SUCCESS('No published posts match — nothing to cull.'))
            return

        self.stdout.write(f'Matched {total} published post(s):\n')
        for marker in (REWRITE_MARKER,) + STATIC_TOPIC_MARKERS + ("sports-auto",):
            count = qs.filter(updated_by=marker).count()
            if count:
                self.stdout.write(f'  {marker}: {count}')

        self.stdout.write('')
        for post in qs.order_by('date_created').values('id', 'title', 'updated_by', 'date_created'):
            date = post['date_created'].strftime('%Y-%m-%d') if post['date_created'] else '????-??-??'
            self.stdout.write(f"  [{post['updated_by']}] {date}  {post['title'][:90]}")

        if not options['apply']:
            self.stdout.write(self.style.WARNING(
                f'\nDry run — no changes made. Re-run with --apply to unpublish these {total} post(s).'
            ))
            return

        category_names = set(
            qs.values_list('categories__name', flat=True).distinct()
        )
        category_names.discard(None)

        unpublished = qs.update(is_published=False)
        self.stdout.write(self.style.SUCCESS(f'\nUnpublished {unpublished} post(s) (now drafts).'))

        try:
            from projectApp.tasks import purge_cloudflare_cache
            result = purge_cloudflare_cache(category_names=sorted(category_names))
            self.stdout.write(f'Cloudflare purge: {result}')
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f'Cloudflare purge failed: {exc}'))

        self.stdout.write(self.style.WARNING(
            'Individual article URLs may stay in the edge cache until TTL — '
            'consider "Purge Everything" in the Cloudflare dashboard after a mass unpublish.'
        ))
