from django.core.management.base import BaseCommand
from django_celery_beat.models import CrontabSchedule, PeriodicTask


TASKS = [
    {
        'name': 'weekly-newsletter',
        'task': 'projectApp.tasks.send_weekly_newsletter',
        'cron': {'minute': '0', 'hour': '8', 'day_of_week': '1',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    {
        'name': 'daily-data-journalism',
        'task': 'projectApp.tasks.generate_data_journalism_articles',
        'cron': {'minute': '0', 'hour': '7', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    # RSS rewrite offset +2h from gov scraper so they never overlap
    {
        'name': 'rss-article-rewrite',
        'task': 'projectApp.tasks.generate_rss_articles',
        'cron': {'minute': '0', 'hour': '2,6,10,14,18,22', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    {
        'name': 'sports-articles',
        'task': 'projectApp.tasks.generate_sports_articles',
        'cron': {'minute': '0', 'hour': '9,21', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    # Market runs 30 min after sports so they don't overlap
    {
        'name': 'market-articles',
        'task': 'projectApp.tasks.generate_market_articles',
        'cron': {'minute': '30', 'hour': '9,21', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    {
        'name': 'politics-articles',
        'task': 'projectApp.tasks.generate_politics_articles',
        'cron': {'minute': '0', 'hour': '11', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    {
        'name': 'entertainment-articles',
        'task': 'projectApp.tasks.generate_entertainment_articles',
        'cron': {'minute': '30', 'hour': '11', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    # Gov scraper is the heaviest task — runs alone at top of each 4-hour block
    {
        'name': 'gov-agency-scraper',
        'task': 'projectApp.tasks.scrape_government_agencies',
        'cron': {'minute': '0', 'hour': '0,4,8,12,16,20', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    {
        'name': 'social-auto-poster',
        'task': 'socialApp.tasks.share_published_posts',
        'cron': {'minute': '*/15', 'hour': '*', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    {
        'name': 'wc2026-match-reports',
        'task': 'projectApp.tasks.generate_wc2026_match_reports',
        'cron': {'minute': '*/30', 'hour': '*', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    {
        'name': 'cbn-news-scraper',
        'task': 'projectApp.tasks.scrape_cbn_news',
        'cron': {'minute': '30', 'hour': '0,4,8,12,16,20', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    {
        'name': 'admin-draft-notifications',
        'task': 'projectApp.tasks.notify_admin_new_drafts',
        'cron': {'minute': '0', 'hour': '*', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    # State House (Presidency) — 4x/day: 6am, 10am, 3pm, 5pm
    {
        'name': 'statehouse-scraper',
        'task': 'projectApp.tasks.scrape_statehouse',
        'cron': {'minute': '0', 'hour': '6,10,15,17', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    # EFCC — 4x/day offset 30 min from statehouse: 6:30am, 10:30am, 3:30pm, 5:30pm
    {
        'name': 'efcc-news-scraper',
        'task': 'projectApp.tasks.scrape_efcc_news',
        'cron': {'minute': '30', 'hour': '6,10,15,17', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    # NNPC — 4x/day offset 45 min: 6:45am, 10:45am, 3:45pm, 5:45pm
    {
        'name': 'nnpc-news-scraper',
        'task': 'projectApp.tasks.scrape_nnpc_news',
        'cron': {'minute': '45', 'hour': '6,10,15,17', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    # NBS — once daily at 7:30am (releases are infrequent; no need for 4x/day)
    {
        'name': 'nbs-data-scraper',
        'task': 'projectApp.tasks.scrape_nbs_data',
        'cron': {'minute': '30', 'hour': '7', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    # NDLEA — 4x/day offset 15 min: 6:15am, 10:15am, 3:15pm, 5:15pm
    {
        'name': 'ndlea-news-scraper',
        'task': 'projectApp.tasks.scrape_ndlea_news',
        'cron': {'minute': '15', 'hour': '6,10,15,17', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
]


class Command(BaseCommand):
    help = 'Register all periodic tasks in the database (safe to re-run)'

    def handle(self, *args, **options):
        created = updated = 0

        for entry in TASKS:
            schedule, _ = CrontabSchedule.objects.get_or_create(**entry['cron'])
            _, was_created = PeriodicTask.objects.update_or_create(
                name=entry['name'],
                defaults={
                    'task': entry['task'],
                    'crontab': schedule,
                    'enabled': True,
                },
            )
            if was_created:
                created += 1
                self.stdout.write(self.style.SUCCESS(f'  Created: {entry["name"]}'))
            else:
                updated += 1
                self.stdout.write(f'  Updated: {entry["name"]}')

        self.stdout.write(self.style.SUCCESS(
            f'\nDone — {created} created, {updated} updated.'
        ))
