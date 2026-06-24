from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projectApp', '0015_alter_post_date_created'),
    ]

    operations = [
        migrations.AddField(
            model_name='post',
            name='publish_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text='Schedule publish date/time. Leave blank to publish immediately.',
            ),
        ),
        migrations.AddField(
            model_name='comment',
            name='is_approved',
            field=models.BooleanField(
                default=False,
                help_text='Only approved comments are shown publicly',
            ),
        ),
    ]
