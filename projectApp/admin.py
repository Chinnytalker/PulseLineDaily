from django.contrib import admin
from django.urls import path
from django.template.response import TemplateResponse
from django.utils.html import format_html

from .models import (
    Post, Category, Comment, NewsletterSubscriber, Video,
    ContactMessage, Job, Advertisement, Author, Applicant,
)


class AuthorAdmin(admin.ModelAdmin):
    list_display = ("name", "email")
    search_fields = ("name", "email")
    prepopulated_fields = {"slug": ("name",)}


class PostAdmin(admin.ModelAdmin):
    list_display = ("title", "is_published", "publish_at", "date_created", "views", "thumbnail", "updated_by")
    search_fields = ("title", "slug")
    list_filter = ('source', "is_published", "date_created", "updated_by")
    prepopulated_fields = {"slug": ("title",)}
    actions = ["approve_posts"]

    class Media:
        css = {
            'all': ('https://cdn.jsdelivr.net/npm/@ckeditor/ckeditor5-build-classic@40.2.0/build/ckeditor.css',)
        }
        js = (
            'https://cdn.jsdelivr.net/npm/@ckeditor/ckeditor5-build-classic@40.2.0/build/ckeditor.js',
            'admin/js/post_editor.js',
        )

    def thumbnail(self, obj):
        if obj.image:
            return format_html('<img src="{}" width="80" height="60" />', obj.image.url)
        return "No Image"
    thumbnail.short_description = "Image"

    def approve_posts(self, request, queryset):
        updated = queryset.update(is_published=True)
        self.message_user(request, f"{updated} post(s) approved and published.")
    approve_posts.short_description = "Approve and publish selected posts"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'analytics/',
                self.admin_site.admin_view(self.analytics_view),
                name='post-analytics',
            ),
        ]
        return custom_urls + urls

    def analytics_view(self, request):
        top_posts = Post.objects.filter(is_published=True).order_by('-views')[:25]
        context = {
            **self.admin_site.each_context(request),
            'top_posts': top_posts,
            'title': 'Post Analytics — Top by Views',
        }
        return TemplateResponse(request, 'admin/post_analytics.html', context)


class CategoryAdmin(admin.ModelAdmin):
    pass


class CommentAdmin(admin.ModelAdmin):
    list_display = ("author", "post", "comment_made_on", "is_approved")
    list_filter = ("is_approved", "comment_made_on")
    search_fields = ("author", "comment")
    actions = ["approve_comments", "reject_comments"]

    def approve_comments(self, request, queryset):
        updated = queryset.update(is_approved=True)
        self.message_user(request, f"{updated} comment(s) approved.")
    approve_comments.short_description = "Approve selected comments"

    def reject_comments(self, request, queryset):
        updated = queryset.update(is_approved=False)
        self.message_user(request, f"{updated} comment(s) rejected.")
    reject_comments.short_description = "Reject selected comments"


class NewsletterSubscriberAdmin(admin.ModelAdmin):
    list_display = ("email", "is_active", "subscribed_on")
    list_editable = ("is_active",)
    search_fields = ("email",)
    list_filter = ("is_active", "subscribed_on")
    actions = ["activate_subscribers", "deactivate_subscribers"]

    def activate_subscribers(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"{updated} subscriber(s) activated.")
    activate_subscribers.short_description = "Activate selected subscribers"

    def deactivate_subscribers(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} subscriber(s) deactivated.")
    deactivate_subscribers.short_description = "Deactivate selected subscribers"


class VideoAdmin(admin.ModelAdmin):
    list_display = ("title", "video_url", "created_at")
    search_fields = ("title",)
    list_filter = ("created_at",)


class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "subject")
    search_fields = ("name", "email", "subject")
    list_filter = ("sent_on",)


class JobAdmin(admin.ModelAdmin):
    list_display = ("title", "location", "employment_type", "active", "posted_on")
    list_filter = ("employment_type", "active", "location")
    search_fields = ("title", "description", "requirements")


class AdvertisementAdmin(admin.ModelAdmin):
    list_display = ("title", "location", "active", "created_at")
    list_filter = ("location", "active",)
    search_fields = ("title",)


class ApplicantAdmin(admin.ModelAdmin):
    list_display = ("applicant_name", "role", "id", "applicant_email", "applied_on",)
    search_fields = ("applicant_name", "applicant_email")
    list_filter = ("applied_on",)


admin.site.register(Post, PostAdmin)
admin.site.register(Category, CategoryAdmin)
admin.site.register(Comment, CommentAdmin)
admin.site.register(NewsletterSubscriber, NewsletterSubscriberAdmin)
admin.site.register(Video, VideoAdmin)
admin.site.register(ContactMessage, ContactMessageAdmin)
admin.site.register(Job, JobAdmin)
admin.site.register(Advertisement, AdvertisementAdmin)
admin.site.register(Author, AuthorAdmin)
admin.site.register(Applicant, ApplicantAdmin)
