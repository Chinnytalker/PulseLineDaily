from django.contrib.syndication.views import Feed
from .models import Post


class LatestPostsFeed(Feed):
    title = "PulseLineDaily"
    link = "/"
    description = "Latest breaking news, politics, sports and more from PulseLineDaily."

    def items(self):
        return Post.published.select_related('author').order_by('-date_created')[:20]

    def item_title(self, item):
        return item.title

    def item_description(self, item):
        return item.summary or item.body[:300]

    def item_link(self, item):
        return item.get_absolute_url()

    def item_pubdate(self, item):
        return item.date_created

    def item_author_name(self, item):
        return item.author.name if item.author else item.updated_by
