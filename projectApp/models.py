
from django.db import models
from django.urls import reverse
from django.utils.text import slugify
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from cloudinary.models import CloudinaryField












class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name
    
    
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
    is_published = models.BooleanField(default=True)
    summary = models.TextField(blank=True, null=True)
    analysis = models.TextField(blank=True, null=True)
    tags = models.CharField(max_length=300, blank=True, null=True, help_text="Comma-separated keywords/tags")
    source_domain = models.CharField(max_length=100, blank=True, null=True)
    reading_time = models.PositiveIntegerField(blank=True, null=True, help_text="Estimated reading time in minutes")
    word_count = models.PositiveIntegerField(blank=True, null=True, help_text="Total word count")
    date_created = models.DateTimeField(auto_now_add=True, blank=True, null=True)
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
    is_breaking = models.BooleanField(default=False, help_text="Mark as breaking news")
    breaking_expiry = models.DateTimeField(blank=True, null=True, help_text="Breaking news expiry time")

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)

        # Auto set expiry if breaking news but no expiry set
        if self.is_breaking and not self.breaking_expiry:
            self.breaking_expiry = timezone.now() + timedelta(hours=6)  # stays for 6 hrs

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

    def __str__(self):
        return self.author



class NewsletterSubscriber(models.Model):
    email = models.EmailField(unique=True)
    subscribed_on = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

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