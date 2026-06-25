from django.urls import path
from . import views
from .feeds import LatestPostsFeed

urlpatterns = [
    path("", views.blog_index, name="Home"),
    path("categories/", views.all_categories, name="all_categories"),
    path("post/<slug:slug>/", views.blog_detail, name="details"),
    path("category/<str:category_name>/", views.blog_category, name="category"),
    path('author/<slug:slug>/', views.author_profile, name='author_profile'),
    path("search/", views.post_search, name="search"),
    path("About/", views.about_us, name="About us"),
    path('privacy/', views.privacy, name='Privacy'),
    path('terms/', views.terms_of_service, name="Terms of service"),
    path('contact_us/', views.contact_us, name="Contact us"),
    path("subscribe/", views.subscribe, name="subscribe"),
    path("careers/", views.careers, name="careers"),
    path("careers/apply/<int:job_id>/", views.job_apply, name="job_apply"),
    path("advertise/", views.advertise, name="advertise"),
    path("internal/posts-for-x/", views.posts_for_x_api, name="posts_for_x_api"),
    # Tag browsing
    path("tag/<str:tag>/", views.tag_page, name="tag"),
    # RSS feed
    path("rss/", LatestPostsFeed(), name="rss_feed"),
    # Reading list / bookmarks
    path("bookmark/<slug:slug>/", views.toggle_bookmark, name="toggle_bookmark"),
    path("saved/", views.saved_posts_view, name="saved_posts"),
    # Author / contributor portal
    path("submit-article/", views.author_submit, name="author_submit"),
    # Newsletter double opt-in confirmation + unsubscribe
    path("newsletter/confirm/<str:token>/", views.newsletter_confirm, name="newsletter_confirm"),
    path("newsletter/unsubscribe/<str:token>/", views.newsletter_unsubscribe, name="newsletter_unsubscribe"),
]
