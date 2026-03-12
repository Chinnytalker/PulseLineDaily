from django.contrib import admin
from .models import Post, Category, Comment, NewsletterSubscriber, Video, ContactMessage, Job, Advertisement, Author, Applicant
from django.utils.html import format_html



class AuthorAdmin(admin.ModelAdmin):
    list_display = ("name", "email")
    search_fields = ("name", "email")
    prepopulated_fields = {"slug": ("name",)}





class PostAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "is_published", "date_created", "thumbnail", "updated_by", "views")
    search_fields = ("title", "slug")
    list_filter = ('source', "is_published", "date_created", "updated_by")
    prepopulated_fields = {"slug": ("title",)}
    actions = ["approve_posts"]

    def thumbnail(self, obj):
        if obj.image:
            return format_html('<img src="{}" width="80" height="60" />', obj.image.url)
        return "No Image"

    thumbnail.short_description = "Image"

    def approve_posts(self, request, queryset):
        updated = queryset.update(is_published=True)
        self.message_user(request, f"{updated} post(s) approved and published.")

class CategoryAdmin(admin.ModelAdmin):
    pass


class CommentAdmin(admin.ModelAdmin):
    list_display = ("author", "comment")

class NewsletterSubscriberAdmin(admin.ModelAdmin):
    list_display = ("email", "is_active")
    search_fields = ("email",)
    list_filter = ("is_active",)


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
