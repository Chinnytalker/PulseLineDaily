from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projectApp', '0018_post_link_max_length_500'),
    ]

    operations = [
        migrations.CreateModel(
            name='GovernmentAgency',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200)),
                ('acronym', models.CharField(blank=True, max_length=20)),
                ('sector', models.CharField(
                    choices=[
                        ('financial', 'Financial & Economic'),
                        ('energy', 'Oil, Gas & Energy'),
                        ('telecom', 'Telecoms & Technology'),
                        ('health', 'Health'),
                        ('transport', 'Transport & Infrastructure'),
                        ('environment', 'Environment & Agriculture'),
                        ('justice', 'Anti-Corruption & Justice'),
                        ('development', 'Development & Investment'),
                        ('education', 'Education'),
                        ('labour', 'Labour, Pension & Immigration'),
                        ('security', 'Security & Defence'),
                        ('elections', 'Elections & Democracy'),
                    ],
                    default='financial',
                    max_length=50,
                )),
                ('website', models.URLField()),
                ('news_url', models.URLField(help_text='Direct URL to the news/press releases page')),
                ('rss_url', models.URLField(blank=True, help_text='RSS feed URL if available', null=True)),
                ('scrape_strategy', models.CharField(
                    choices=[('rss', 'RSS Feed'), ('html', 'HTML Scrape')],
                    default='html',
                    max_length=10,
                )),
                ('is_active', models.BooleanField(default=True)),
                ('last_scraped_at', models.DateTimeField(blank=True, null=True)),
                ('priority', models.PositiveIntegerField(default=1, help_text='Lower number = higher priority')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'Government Agency',
                'verbose_name_plural': 'Government Agencies',
                'ordering': ['priority', 'name'],
            },
        ),
    ]
