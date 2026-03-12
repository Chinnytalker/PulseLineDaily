from django.urls import path

from . import views

urlpatterns = [
    # ...existing url patterns...
    path("", views.blog_index, name="Home"),
    path("post/<slug:slug>/", views.blog_detail, name="details"),
    path("category/<str:category_name>/", views.blog_category, name="category"),
    path('author/<slug:slug>/', views.author_profile, name='author_profile'),
    path("search/", views.post_search, name="search"),
    path("About/", views.about_us, name="About us"),
    path('privacy/', views.privacy, name='Privacy'),
    path('terms/', views.terms_of_service, name="Terms of service"),
    path('contact_us/', views.contact_us, name="Contact us"),
    # path('api/fetch-news/', views.fetch_news_api, name='fetch_news_api'),   # API endpoint to fetch news
    path("subscribe/", views.subscribe, name="subscribe"),
    path("careers/", views.careers, name="careers"),
    path("careers/apply/<int:job_id>/", views.job_apply, name="job_apply"),
    path("advertise/", views.advertise, name="advertise"),
]

