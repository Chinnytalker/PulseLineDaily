from django.contrib import admin
from .models import SocialPostLog


@admin.register(SocialPostLog)
class SocialPostLogAdmin(admin.ModelAdmin):
    list_display = ('platform', 'post_title', 'success', 'shared_at', 'platform_post_id')
    list_filter = ('platform', 'success', 'shared_at')
    search_fields = ('post__title', 'platform_post_id', 'error_message')
    readonly_fields = ('post', 'platform', 'shared_at', 'success', 'platform_post_id', 'error_message')
    ordering = ('-shared_at',)

    def post_title(self, obj):
        return obj.post.title[:80]
    post_title.short_description = 'Post'

    def has_add_permission(self, request):
        return False
