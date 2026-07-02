from django.db import models


class SocialPostLog(models.Model):
    PLATFORM_CHOICES = [
        ('facebook', 'Facebook'),
        ('telegram', 'Telegram'),
        ('onesignal', 'Push Notification'),
    ]

    post = models.ForeignKey(
        'projectApp.Post',
        on_delete=models.CASCADE,
        related_name='social_logs',
    )
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    shared_at = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField(default=False)
    platform_post_id = models.CharField(max_length=300, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ['-shared_at']
        verbose_name = 'Social Post Log'
        verbose_name_plural = 'Social Post Logs'

    def __str__(self):
        status = 'OK' if self.success else 'FAILED'
        return f'{self.get_platform_display()} | {self.post.title[:60]} | {status}'
