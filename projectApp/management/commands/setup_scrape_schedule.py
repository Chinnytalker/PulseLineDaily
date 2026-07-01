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
    {
        'name': 'rss-article-rewrite',
        'task': 'projectApp.tasks.generate_rss_articles',
        'cron': {'minute': '0', 'hour': '6,14,22', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    {
        'name': 'sports-articles',
        'task': 'projectApp.tasks.generate_sports_articles',
        'cron': {'minute': '0', 'hour': '9,21', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    {
        'name': 'market-articles',
        'task': 'projectApp.tasks.generate_market_articles',
        'cron': {'minute': '30', 'hour': '8,20', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    {
        'name': 'politics-articles',
        'task': 'projectApp.tasks.generate_politics_articles',
        'cron': {'minute': '0', 'hour': '10', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    {
        'name': 'entertainment-articles',
        'task': 'projectApp.tasks.generate_entertainment_articles',
        'cron': {'minute': '0', 'hour': '11', 'day_of_week': '*',
                 'day_of_month': '*', 'month_of_year': '*'},
    },
    {
        'name': 'gov-agency-scraper',
        'task': 'projectApp.tasks.scrape_government_agencies',
        'cron': {'minute': '0', 'hour': '7,13,19', 'day_of_week': '*',
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
