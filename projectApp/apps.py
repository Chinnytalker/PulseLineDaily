from django.apps import AppConfig


class ProjectappConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'projectApp'


    def ready(self):
        # Import here to avoid circular imports
        from django.db.models.signals import post_migrate
        from django.core.management import call_command

        def setup_scrape_task(sender, **kwargs):
            try:
                # Run your setup command automatically after migrations
                call_command('setup_scrape_schedule')
                print("✅ Scraping schedule ensured (every 8 hours).")
            except Exception as e:
                print(f"⚠ Could not setup scraping schedule automatically: {e}")

        # Connect the signal to post_migrate
        # post_migrate.connect(setup_scrape_task, sender=self)
