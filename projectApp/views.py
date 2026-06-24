import uuid
import logging

from django.contrib import messages
from django.core.cache import cache
from django.core.mail import send_mail, EmailMultiAlternatives
from django.shortcuts import render, get_object_or_404, redirect
from django.core import signing
from django.template.loader import render_to_string
from django.core.paginator import Paginator, PageNotAnInteger, EmptyPage
from django.db.models import Q
from django.http import HttpResponseRedirect, JsonResponse, HttpResponseForbidden, Http404
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt  # kept for posts_for_x_api if ever needed
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.dateparse import parse_datetime

from projectApp.models import Post, Comment, Category, NewsletterSubscriber, Video, Job, Author
from projectApp.forms import (
    CommentForm, SearchForm, ContactForm, NewsletterSubscriptionForm,
    JobApplicationForm, AdvertisementRequestForm, DraftSubmissionForm,
)

logger = logging.getLogger(__name__)

# Session key constants — change in one place only
SESSION_VIEWED = 'viewed_posts'
SESSION_SAVED  = 'saved_posts'

HOMEPAGE_CACHE_TTL = 300  # seconds (5 minutes)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return forwarded.split(',')[0].strip() if forwarded else request.META.get('REMOTE_ADDR', '')


def _is_rate_limited(request, action, limit=5, period=300):
    """Return True and block if this IP has exceeded `limit` POST attempts in `period` seconds."""
    key = f'rl:{action}:{_client_ip(request)}'
    count = cache.get(key, 0)
    if count >= limit:
        return True
    cache.set(key, count + 1, period)
    return False


def _icontains_search(query):
    return (
        Post.published
        .filter(
            Q(title__icontains=query) |
            Q(body__icontains=query) |
            Q(author__name__icontains=query) |
            Q(tags__icontains=query) |
            Q(categories__name__icontains=query)
        )
        .distinct()
        .order_by('-date_created')
    )


# ── Public views ───────────────────────────────────────────────────────────────

def blog_index(request):
    post_list = Post.published.order_by("-date_created")
    paginator = Paginator(post_list, 15)
    posts = paginator.get_page(request.GET.get("page"))

    trending_posts = cache.get('hp_trending')
    if trending_posts is None:
        trending_posts = list(
            Post.published.select_related('author').order_by("-date_created")[:10]
        )
        cache.set('hp_trending', trending_posts, HOMEPAGE_CACHE_TTL)

    category_posts = cache.get('hp_categories')
    if category_posts is None:
        category_posts = {}
        for category in Category.objects.all():
            category_posts[category] = list(
                Post.published
                .filter(categories=category)
                .select_related('author')
                .order_by("-date_created")[:10]
            )
        cache.set('hp_categories', category_posts, HOMEPAGE_CACHE_TTL)

    breaking_news = cache.get('hp_breaking')
    if breaking_news is None:
        breaking_news = list(
            Post.published.filter(
                is_breaking=True,
                breaking_expiry__gte=timezone.now()
            ).order_by("-date_created")[:5]
        )
        cache.set('hp_breaking', breaking_news, HOMEPAGE_CACHE_TTL)

    context = {
        "posts": posts,
        "trending_posts": trending_posts,
        "category_posts": category_posts,
        "breaking_news": breaking_news,
        "videos": Video.objects.last(),
    }
    return render(request, "blog/index.html", context)


def blog_category(request, category_name):
    category = get_object_or_404(Category, name=category_name)
    qs = Post.published.filter(categories=category).select_related('author').order_by("-date_created")
    paginator = Paginator(qs, 15)
    try:
        posts = paginator.get_page(request.GET.get('page'))
    except PageNotAnInteger:
        posts = paginator.page(1)
    except EmptyPage:
        posts = paginator.page(paginator.num_pages)
    return render(request, "blog/category.html", {"category": category, "posts": posts})


def author_profile(request, slug):
    author = get_object_or_404(Author, slug=slug)
    qs = Post.published.filter(author=author).order_by('-date_created')
    paginator = Paginator(qs, 12)
    recent_posts = paginator.get_page(request.GET.get('page'))
    return render(request, 'blog/author_profile.html', {'author': author, 'recent_posts': recent_posts})


def blog_detail(request, slug):
    # Use published manager so scheduled posts (publish_at > now) are inaccessible by URL
    try:
        post = Post.published.get(slug=slug)
    except Post.DoesNotExist:
        raise Http404

    # Deduplicate view counts per session
    viewed = request.session.get(SESSION_VIEWED, [])
    if post.id not in viewed:
        post.increment_views()
        viewed.append(post.id)
        request.session[SESSION_VIEWED] = viewed
        request.session.modified = True

    form = CommentForm()
    if request.method == "POST":
        form = CommentForm(request.POST)
        if form.is_valid():
            Comment.objects.create(
                author=form.cleaned_data["author"],
                comment=form.cleaned_data["body"],
                post=post,
                is_approved=False,
            )
            messages.success(request, "Your comment has been submitted and is pending approval.")
            return HttpResponseRedirect(request.path_info)

    related_posts = (
        Post.published
        .filter(categories__in=post.categories.all())
        .exclude(id=post.id)
        .distinct()
        .order_by('-date_created')[:4]
    )

    tags = [t.strip() for t in post.tags.split(",") if t.strip()] if post.tags else []
    saved_ids = request.session.get(SESSION_SAVED, [])

    context = {
        "post": post,
        "comments": Comment.objects.filter(post=post, is_approved=True),
        "form": CommentForm(),
        "related_posts": related_posts,
        "tags": tags,
        "is_bookmarked": post.id in saved_ids,
    }
    return render(request, "blog/detail.html", context)


def post_search(request):
    form = SearchForm(request.GET or None)
    query = request.GET.get('query', '').strip()
    search_post = Post.objects.none()

    if query:
        db_engine = settings.DATABASES['default']['ENGINE']
        if 'postgresql' in db_engine:
            try:
                from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank
                sv = (
                    SearchVector('title', weight='A') +
                    SearchVector('tags', weight='A') +
                    SearchVector('summary', weight='B') +
                    SearchVector('body', weight='C')
                )
                sq = SearchQuery(query)
                search_post = (
                    Post.published
                    .annotate(search=sv, rank=SearchRank(sv, sq))
                    .filter(search=sq)
                    .order_by('-rank', '-date_created')
                    .distinct()
                )
            except Exception:
                search_post = _icontains_search(query)
        else:
            search_post = _icontains_search(query)

    paginator = Paginator(search_post, 15)
    page_number = request.GET.get('page')
    try:
        results = paginator.get_page(page_number)
    except PageNotAnInteger:
        results = paginator.page(1)
    except EmptyPage:
        results = paginator.page(paginator.num_pages)

    return render(request, 'blog/post_search.html', {'form': form, 'query': query, 'results': results})


def tag_page(request, tag):
    tag = tag.strip()
    qs = Post.published.filter(tags__icontains=tag).select_related('author').order_by('-date_created')
    paginator = Paginator(qs, 15)
    posts = paginator.get_page(request.GET.get('page'))
    return render(request, 'blog/tag.html', {'tag': tag, 'posts': posts})


def toggle_bookmark(request, slug):
    post = get_object_or_404(Post.published.all(), slug=slug)
    saved = request.session.get(SESSION_SAVED, [])
    if post.id in saved:
        saved.remove(post.id)
        saved_status = False
    else:
        saved.append(post.id)
        saved_status = True
    request.session[SESSION_SAVED] = saved
    request.session.modified = True

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'saved': saved_status})
    return redirect(request.META.get('HTTP_REFERER') or '/')


def saved_posts_view(request):
    saved_ids = request.session.get(SESSION_SAVED, [])
    posts = Post.published.filter(id__in=saved_ids).select_related('author') if saved_ids else []
    return render(request, 'blog/saved_posts.html', {'posts': posts})


def about_us(request):
    return render(request, 'blog/about_us.html')


def terms_of_service(request):
    return render(request, 'blog/terms_of_service.html')


def privacy(request):
    return render(request, 'blog/privacy.html')


def contact_us(request):
    if request.method == 'POST':
        if _is_rate_limited(request, 'contact', limit=5, period=300):
            messages.error(request, "Too many submissions. Please wait a few minutes and try again.")
            return redirect("Contact us")
        form = ContactForm(request.POST)
        if form.is_valid():
            msg = form.save()
            send_mail(
                subject=f"[PulseLineDaily] Contact: {msg.subject}",
                message=(
                    f"Name: {msg.name}\n"
                    f"Email: {msg.email}\n"
                    f"Subject: {msg.subject}\n\n"
                    f"{msg.message}"
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[settings.CONTACT_EMAIL],
                fail_silently=False,
            )
            messages.success(request, "Message sent successfully!")
            return redirect("Contact us")
    else:
        form = ContactForm()
    return render(request, "blog/contact_us.html", {"form": form})


def subscribe(request):
    if request.method == "POST":
        if _is_rate_limited(request, 'subscribe', limit=3, period=300):
            messages.error(request, "Too many attempts. Please try again in a few minutes.")
            return redirect(request.META.get("HTTP_REFERER") or "/")

        form = NewsletterSubscriptionForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email']
            existing = NewsletterSubscriber.objects.filter(email=email).first()

            if existing and existing.is_active:
                messages.info(request, "You are already subscribed!")
            else:
                token = uuid.uuid4().hex
                if existing:
                    existing.confirmation_token = token
                    existing.is_active = False
                    existing.save(update_fields=['confirmation_token', 'is_active'])
                else:
                    NewsletterSubscriber.objects.create(
                        email=email, confirmation_token=token, is_active=False
                    )
                confirm_url = request.build_absolute_uri(
                    reverse('newsletter_confirm', args=[token])
                )
                html_body = render_to_string('blog/email/newsletter_confirm.html', {
                    'confirm_url': confirm_url,
                })
                plain_body = (
                    f"Hi,\n\nThank you for subscribing to PulseLineDaily!\n\n"
                    f"Confirm your email here:\n{confirm_url}\n\n"
                    f"If you didn't subscribe, ignore this email.\n\n"
                    f"— The PulseLineDaily Team"
                )
                msg = EmailMultiAlternatives(
                    subject="Confirm your PulseLineDaily subscription",
                    body=plain_body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[email],
                )
                msg.attach_alternative(html_body, "text/html")
                msg.send(fail_silently=False)
                messages.success(request, "Almost there! Check your email to confirm your subscription.")
    return redirect(request.META.get("HTTP_REFERER") or "/")


def newsletter_confirm(request, token):
    subscriber = get_object_or_404(NewsletterSubscriber, confirmation_token=token)
    subscriber.is_active = True
    subscriber.confirmation_token = None
    subscriber.save(update_fields=['is_active', 'confirmation_token'])
    return render(request, 'blog/newsletter_confirm.html', {'email': subscriber.email})


def newsletter_unsubscribe(request, token):
    try:
        email = signing.loads(token, salt='newsletter-unsub')
    except signing.BadSignature:
        raise Http404
    NewsletterSubscriber.objects.filter(email=email).update(is_active=False)
    return render(request, 'blog/newsletter_unsubscribe.html', {'email': email})


def careers(request):
    jobs = Job.objects.filter(active=True).order_by('-posted_on')
    return render(request, 'blog/careers.html', {'jobs': jobs})


def job_apply(request, job_id):
    job = get_object_or_404(Job, id=job_id, active=True)
    if request.method == 'POST':
        if _is_rate_limited(request, 'job_apply', limit=5, period=300):
            messages.error(request, "Too many submissions. Please try again later.")
            return redirect('careers')
        form = JobApplicationForm(request.POST, request.FILES)
        if form.is_valid():
            application = form.save(commit=False)
            application.role = job
            application.save()
            messages.success(request, 'Your application has been submitted!')
            return redirect('careers')
    else:
        form = JobApplicationForm()
    return render(request, 'blog/job_apply.html', {'form': form, 'job': job})


def advertise(request):
    if request.method == 'POST':
        if _is_rate_limited(request, 'advertise', limit=5, period=300):
            messages.error(request, "Too many submissions. Please try again later.")
            return redirect('advertise')
        form = AdvertisementRequestForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            send_mail(
                subject=f"[PulseLineDaily] Ad Request from {data['advertiser_name']}",
                message=(
                    f"Name: {data['advertiser_name']}\n"
                    f"Email: {data['advertiser_email']}\n"
                    f"Preferred Location: {data['preferred_location']}\n"
                    f"Ad URL: {data.get('ad_url') or 'N/A'}\n"
                    f"Message: {data.get('message') or 'N/A'}"
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[settings.CONTACT_EMAIL],
                fail_silently=True,
            )
            messages.success(request, 'Your advertisement request has been submitted!')
            return redirect('advertise')
    else:
        form = AdvertisementRequestForm()
    return render(request, 'blog/advertise.html', {'form': form})


def author_submit(request):
    if request.method == 'POST':
        form = DraftSubmissionForm(request.POST)
        if form.is_valid():
            draft = form.save(commit=False)
            draft.is_published = False
            draft.source = 'manual'
            draft.updated_by = form.cleaned_data.get('contributor_name', 'Contributor')
            draft.save()
            form.save_m2m()
            messages.success(request, "Article submitted for review. We'll be in touch!")
            return redirect('author_submit')
    else:
        form = DraftSubmissionForm()
    return render(request, 'blog/author_submit.html', {'form': form})


# ── Internal API ───────────────────────────────────────────────────────────────

def posts_for_x_api(request):
    request_token = request.headers.get("X-Internal-Token", "")
    expected_token = getattr(settings, "INTERNAL_API_TOKEN", "")

    if not request_token or not expected_token:
        logger.warning("X API denied: missing token or server token not configured.")
        return HttpResponseForbidden("Forbidden")

    if not constant_time_compare(request_token, expected_token):
        logger.warning("X API denied: invalid token.")
        return HttpResponseForbidden("Forbidden")

    since = request.GET.get("since")
    try:
        limit = min(max(int(request.GET.get("limit", "20")), 1), 50)
    except ValueError:
        limit = 20

    posts = (
        Post.published
        .filter(slug__isnull=False)
        .exclude(slug="")
        .only("id", "title", "slug", "summary", "date_created")
    )

    if since:
        parsed_since = parse_datetime(since)
        if parsed_since:
            if timezone.is_naive(parsed_since):
                parsed_since = timezone.make_aware(parsed_since, timezone.get_current_timezone())
            posts = posts.filter(date_created__gt=parsed_since)

    posts = posts.order_by("date_created")[:limit]

    data = [
        {
            "id": p.id,
            "title": p.title,
            "slug": p.slug,
            "summary": p.summary or "",
            "link": request.build_absolute_uri(p.get_absolute_url()),
            "date_created": p.date_created.isoformat() if p.date_created else None,
        }
        for p in posts
    ]

    logger.info("X API served: since=%s limit=%s returned=%s", since, limit, len(data))
    return JsonResponse({"success": True, "count": len(data), "posts": data})
