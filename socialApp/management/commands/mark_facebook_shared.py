from django.core.management.base import BaseCommand
from django.utils import timezone

from projectApp.models import Post
from socialApp.models import SocialPostLog


class Command(BaseCommand):
    help = (
        'Mark all existing published posts as already shared to Facebook. '
        'Run this ONCE on production before enabling the social auto-poster '
        'to prevent re-sharing posts you already posted manually.'
    )

    def handle(self, *args, **options):
        posts = Post.published.all()
        total = posts.count()

        if total == 0:
            self.stdout.write('No published posts found.')
            return

        created = 0
        skipped = 0

        for post in posts:
            _, was_created = SocialPostLog.objects.get_or_create(
                post=post,
                platform='facebook',
                defaults={
                    'success': True,
                    'platform_post_id': 'pre-existing',
                },
            )
            if was_created:
                created += 1
            else:
                skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f'\nDone — {created} posts marked as already shared to Facebook '
            f'({skipped} already had a log entry).'
        ))
        self.stdout.write(
            'The auto-poster will now only share NEW posts published after this point.'
        )
