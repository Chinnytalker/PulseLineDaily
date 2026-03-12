from django.contrib import messages
from django.core.mail import send_mail
from django.shortcuts import render, get_object_or_404, redirect
from projectApp.models import Post, Comment, Category, NewsletterSubscriber, Video, Job, Advertisement, Author
from django.db.models import Q
from projectApp.forms import CommentForm, SearchForm, ContactForm, NewsletterSubscriptionForm, JobApplicationForm, AdvertisementRequestForm
from django.http import HttpResponseRedirect, JsonResponse, HttpResponseForbidden
from django.core.paginator import Paginator, PageNotAnInteger, EmptyPage
from django.utils.safestring import mark_safe
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
# from projectApp.utils.fetch_and_summarize import scrape_and_save_domains
from django.urls import reverse
from django.utils import timezone
# from projectApp.tasks import scrape_domains_every_8hrs




def blog_index(request):
    # All posts with pagination
    post_list = Post.objects.filter(is_published=True).order_by("-date_created")
    paginator = Paginator(post_list, 15)
    page_number = request.GET.get("page")
    posts = paginator.get_page(page_number)

    # Just trending posts based on views
    trending_posts = Post.objects.filter(is_published=True).order_by("-views")[:5]

    # Categories section
    categories = Category.objects.all()
    category_posts = {}
    for category in categories:
        category_posts[category] = Post.objects.filter(
            categories=category, is_published=True
        ).order_by("-date_created")[:10]

    # 🔥 Breaking news logic
    breaking_news = Post.objects.filter(
        is_breaking=True, is_published=True,
        breaking_expiry__gte=timezone.now()
    ).order_by("-date_created")[:5]

    # Videos
    videos = Video.objects.last()

    context = {
        "posts": posts,
        "trending_posts": trending_posts,
        "category_posts": category_posts,
        "breaking_news": breaking_news,
        "videos": videos,
    }
    return render(request, "blog/index.html", context)



def blog_category(request, category_name):
    category = get_object_or_404(Category, name=category_name)
    category_post = Post.objects.filter(categories=category, is_published=True).order_by("-date_created")
    paginator = Paginator(category_post, 15)
    category_number = request.GET.get('page')
    posts = paginator.get_page(category_number)
    try:
        posts = paginator.get_page(category_number)
    except PageNotAnInteger:
        posts = paginator.page(1)
    except EmptyPage:
        posts = paginator.page(paginator.num_pages)
    context = {
        "category": category,
        "posts": posts,
    }
    return render(request, "blog/category.html", context)


# Author profile view
def author_profile(request, slug):
    author = get_object_or_404(Author, slug=slug)
    recent_posts = author.posts.order_by('-date_created')[:10]
    context = {
        'author': author,
        'recent_posts': recent_posts,
    }
    return render(request, 'blog/author_profile.html', context)


def blog_detail(request, slug):
    post = get_object_or_404(Post, slug=slug)
    post.increment_views()
    form = CommentForm()

    if request.method == "POST":
        form = CommentForm(request.POST)
        if form.is_valid():
            comment = Comment(
                author=form.cleaned_data["author"],
                comment=form.cleaned_data["body"],
                post=post,
            )
            comment.save()
            return HttpResponseRedirect(mark_safe(request.path_info))

    comments = Comment.objects.filter(post=post)

    # Related posts: same categories, exclude current post, order by recent
    related_posts = Post.objects.filter(
        categories__in=post.categories.all(), is_published=True
    ).exclude(id=post.id).distinct().order_by('-date_created')[:4]

    # ✅ Split tags safely (avoids template error)
    tags = []
    if post.tags:
        tags = [tag.strip() for tag in post.tags.split(",") if tag.strip()]

    context = {
        "post": post,
        "comments": comments,
        "form": CommentForm(),
        "related_posts": related_posts,
        "tags": tags,  # ← pass to template
    }
    return render(request, "blog/detail.html", context)




def post_search(request):
    form = SearchForm(request.GET or None)
    query = request.GET.get('query', '')
    results = []

    if form.is_valid() and form.cleaned_data.get('query'):
        query = form.cleaned_data['query']
        search_post = Post.objects.filter(
            is_published=True).filter(
            Q(title__icontains=query) |
            Q(body__icontains=query) |
            Q(author__name__icontains=query)
        ).order_by('-date_created')
    else:
        search_post = Post.objects.none()  # Empty queryset if no search

    paginator = Paginator(search_post, 15)
    page_number = request.GET.get('page')
    try:
        results = paginator.get_page(page_number)
    except PageNotAnInteger:
        results = paginator.page(1)
    except EmptyPage:
        results = paginator.page(paginator.num_pages)

    context = {
        'form': form,
        'query': query,
        'results': results,
    }
    return render(request, 'blog/post_search.html', context)


def about_us(request):
    return render(request, 'blog/about_us.html', )


def terms_of_service(request):
    return render(request, 'blog/terms_of_service.html', )


def privacy(request):
    return render(request,'blog/privacy.html',)


def contact_us(request):
    if request.method == 'POST':
        form = ContactForm(request.POST)
        if form.is_valid():
            # Save to database
            contact_message = form.save()

            # Send notification email
            send_mail(
                subject=f"New Contact: {contact_message.subject}",
                message=f"From: {contact_message.name} <{contact_message.email}>\n\n{contact_message.message}",
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=['nwachukwuclinton2@gmail.com'],  # your email
                fail_silently=False,
            )

            # Success feedback
            messages.success(request, "Your message has been sent successfully ✅")
            return redirect("contact_us")  # prevents form resubmission
    else:
        form = ContactForm()

    return render(request, "blog/contact_us.html", {"form": form})


def subscribe(request):
    if request.method == "POST":
        form = NewsletterSubscriptionForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email']
            if not NewsletterSubscriber.objects.filter(email=email).exists():
                NewsletterSubscriber.objects.create(email=email)
                messages.success(request, "Subscribed successfully ✅")
    # Always redirect back to where the form was submitted from
    return redirect(request.META.get("HTTP_REFERER", "/"))



# @csrf_exempt
# def fetch_news_api(request):
#     if request.method == "POST" and request.headers.get("X-CRON-SECRET") == getattr(settings, "CRON_SECRET", None):
#         scrape_and_save_domains()
#         return JsonResponse({"status": "success"})
#     return HttpResponseForbidden("Forbidden")


def careers(request):
    jobs = Job.objects.filter(active=True).order_by('-posted_on')
    return render(request, 'blog/careers.html', {'jobs': jobs})

# Job Application
def job_apply(request, job_id):
    job = get_object_or_404(Job, id=job_id, active=True)
    if request.method == 'POST':
        form = JobApplicationForm(request.POST, request.FILES)
        if form.is_valid():
            application = form.save(commit=False)
            application.job = job
            application.save()
            # Here you could add additional logic, like
            # saving the application to a separate model, or email it, etc.
            messages.success(request, 'Your application has been submitted!')
            return redirect('careers')
    else:
        form = JobApplicationForm()
    return render(request, 'blog/job_apply.html', {'form': form, 'job': job})

# Advertisement Request
def advertise(request):
    if request.method == 'POST':
        form = AdvertisementRequestForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, 'Your advertisement request has been submitted!')
            return redirect('advertise')
    else:
        form = AdvertisementRequestForm()
    return render(request, 'blog/advertise.html', {'form': form})




