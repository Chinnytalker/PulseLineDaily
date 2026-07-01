from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('projectApp', '0019_governmentagency'),
    ]

    operations = [
        migrations.CreateModel(
            name='SocialPostLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('platform', models.CharField(choices=[('facebook', 'Facebook'), ('telegram', 'Telegram')], max_length=20)),
                ('shared_at', models.DateTimeField(auto_now_add=True)),
                ('success', models.BooleanField(default=False)),
                ('platform_post_id', models.CharField(blank=True, max_length=300)),
                ('error_message', models.TextField(blank=True)),
                ('post', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='social_logs',
                    to='projectApp.post',
                )),
            ],
            options={
                'verbose_name': 'Social Post Log',
                'verbose_name_plural': 'Social Post Logs',
                'ordering': ['-shared_at'],
            },
        ),
    ]
