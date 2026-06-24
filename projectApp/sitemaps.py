from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from .models import Author, Category, Job, Post


class StaticViewSitemap(Sitemap):
    protocol = "https"
    changefreq = "weekly"
    priority = 0.7

    def items(self):
        return [
            "Home",
            "About us",
            "Privacy",
            "Terms of service",
            "Contact us",
            "careers",
            "advertise",
        ]

    def location(self, item):
        return reverse(item)


class PostSitemap(Sitemap):
    protocol = "https"
    changefreq = "daily"
    priority = 0.9

    def items(self):
        return (
            Post.published
            .filter(slug__isnull=False)
            .exclude(slug="")
            .order_by("-date_created")
        )

    def lastmod(self, obj):
        return obj.last_modified


class CategorySitemap(Sitemap):
    protocol = "https"
    changefreq = "daily"
    priority = 0.6

    def items(self):
        return Category.objects.order_by("name")

    def location(self, obj):
        return reverse("category", args=[obj.name])


class AuthorSitemap(Sitemap):
    protocol = "https"
    changefreq = "weekly"
    priority = 0.5

    def items(self):
        return Author.objects.filter(slug__isnull=False).exclude(slug="").order_by("name")

    def lastmod(self, obj):
        latest_post = obj.posts.filter(is_published=True).order_by("-last_modified").first()
        return latest_post.last_modified if latest_post else None


class JobSitemap(Sitemap):
    protocol = "https"
    changefreq = "weekly"
    priority = 0.5

    def items(self):
        return Job.objects.filter(active=True).order_by("-posted_on")

    def lastmod(self, obj):
        return obj.posted_on

    def location(self, obj):
        return reverse("job_apply", args=[obj.pk])


sitemaps = {
    "static": StaticViewSitemap,
    "posts": PostSitemap,
    "categories": CategorySitemap,
    "authors": AuthorSitemap,
    "jobs": JobSitemap,
}
