
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils.text import slugify
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from cloudinary.models import CloudinaryField


class PublishedPostManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(
            is_published=True
        ).filter(
            Q(publish_at__isnull=True) | Q(publish_at__lte=timezone.now())
        )












class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        from django.core.cache import cache
        super().save(*args, **kwargs)
        cache.delete_many(['hp_categories', 'all_categories_page'])

    def delete(self, *args, **kwargs):
        from django.core.cache import cache
        super().delete(*args, **kwargs)
        cache.delete_many(['hp_categories', 'all_categories_page'])
    
    
class Author(models.Model):
    name = models.CharField(max_length=100, unique=True)
    bio = models.TextField(blank=True, null=True)
    photo = CloudinaryField('image', blank=True, null=True)  # ✅ Changed
    email = models.EmailField(blank=True, null=True)
    slug = models.SlugField(unique=True, blank=True, null=True)
    social_profile = models.URLField(blank=True, null=True, help_text="Link to author's social media (Twitter, LinkedIn, etc.)")
    

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('author_profile', args=[str(self.slug)])

    def __str__(self):
        return self.name



class Post(models.Model):
    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, blank=True, null=True, max_length=200)
    image = CloudinaryField('image', blank=True, null=True)
    video = CloudinaryField('video', resource_type='video', blank=True, null=True)
    link = models.URLField(blank=True, null=True)
    body = models.TextField()
    is_published = models.BooleanField(default=True, db_index=True)
    summary = models.TextField(blank=True, null=True)
    analysis = models.TextField(blank=True, null=True)
    tags = models.CharField(max_length=300, blank=True, null=True, help_text="Comma-separated keywords/tags")
    source_domain = models.CharField(max_length=100, blank=True, null=True)
    reading_time = models.PositiveIntegerField(blank=True, null=True, help_text="Estimated reading time in minutes")
    word_count = models.PositiveIntegerField(blank=True, null=True, help_text="Total word count")
    date_created = models.DateTimeField(auto_now_add=True, blank=True, null=True, db_index=True)
    last_modified = models.DateTimeField(auto_now=True)
    views = models.PositiveIntegerField(default=0)
    categories = models.ManyToManyField("Category", related_name='posts')
    author = models.ForeignKey('Author', on_delete=models.SET_NULL, null=True, blank=True, related_name='posts')
    updated_by = models.CharField(max_length=100, default='Clinton Nwachukwu')

    SOURCE_CHOICES = [
        ('manual', 'Manual'),
        ('api', 'API'),
        ('scrape', 'Scrape'),
    ]
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='manual')

    # 🔥 Breaking news fields
    is_breaking = models.BooleanField(default=False, db_index=True, help_text="Mark as breaking news")
    breaking_expiry = models.DateTimeField(blank=True, null=True, help_text="Breaking news expiry time")

    publish_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text="Schedule publish date/time. Leave blank to publish immediately."
    )

    objects = models.Manager()
    published = PublishedPostManager()

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title)
            slug, counter = base, 1
            while Post.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{counter}"
                counter += 1
            self.slug = slug

        # Auto-compute word count and reading time from body (~200 wpm)
        if self.body:
            words = len(self.body.split())
            self.word_count = words
            self.reading_time = max(1, round(words / 200))

        # Auto set expiry if breaking news but no expiry set
        if self.is_breaking and not self.breaking_expiry:
            self.breaking_expiry = timezone.now() + timedelta(hours=6)

        super().save(*args, **kwargs)

    @property
    def is_still_breaking(self):
        """Check if still valid breaking news"""
        return self.is_breaking and (not self.breaking_expiry or self.breaking_expiry > timezone.now())

    def increment_views(self):
        self.views += 1
        self.save(update_fields=["views"])

    def get_absolute_url(self):
        return reverse('details', args=[str(self.slug)])

    def __str__(self):
        return self.title

class Comment(models.Model):
    author = models.CharField(max_length=100)
    comment = models.TextField()
    comment_made_on = models.DateTimeField(auto_now_add=True)
    post = models.ForeignKey(Post, on_delete=models.CASCADE)
    is_approved = models.BooleanField(default=False, db_index=True, help_text="Only approved comments are shown publicly")

    def __str__(self):
        return self.author



class NewsletterSubscriber(models.Model):
    email = models.EmailField(unique=True)
    subscribed_on = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=False, help_text="True only after email confirmation")
    confirmation_token = models.CharField(max_length=64, blank=True, null=True, unique=True)

    def __str__(self):
        return self.email
    
    
    
    
class ContactMessage(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField()
    subject = models.CharField(max_length=200)
    message = models.TextField()
    sent_on = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Message from {self.name} - {self.subject}"
    
    
    
class Video(models.Model):
    title = models.CharField(max_length=200)
    video_url = models.URLField()
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.title  

class Job(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField()
    location = models.CharField(max_length=100, blank=True, null=True)
    employment_type = models.CharField(
        max_length=50,
        choices=[
            ('full-time', 'Full-Time'),
            ('part-time', 'Part-Time'),
            ('contract', 'Contract'),
            ('internship', 'Internship'),
        ],
        default='full-time'
    )
    requirements = models.TextField(blank=True, null=True)
    posted_on = models.DateTimeField(auto_now_add=True)
    active = models.BooleanField(default=True)
    cover_letter = models.TextField(blank=True, null=True)
    resume = CloudinaryField('file', blank=True, null=True, resource_type='raw')  # ✅ Changed (resume uploads)
    
    def __str__(self):
        return self.title
    
    
    
class Advertisement(models.Model):
    title = models.CharField(max_length=200)
    image = CloudinaryField('image', blank=True, null=True)
    link = models.URLField(blank=True, null=True)
    location = models.CharField(
        max_length=50,
        choices=[
            ('header', 'Header'),
            ('sidebar', 'Sidebar'),
            ('footer', 'Footer'),
        ],
        default='sidebar'
    )
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.title


class Applicant(models.Model):
    applicant_name = models.CharField(max_length=200, default=None, )
    applicant_email = models.EmailField()
    cover_letter = models.TextField(blank=True, null=True, default=None)
    applied_on = models.DateTimeField(auto_now=True,)
    resume = CloudinaryField('file', blank=True, null=True, resource_type='raw')
    role = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='applicants', null=True, blank=True)


    def __str__(self):
        return self.applicant_name