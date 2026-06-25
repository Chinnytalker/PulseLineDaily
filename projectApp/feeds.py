from django.contrib.syndication.views import Feed
from django.utils.feedgenerator import Rss201rev2Feed
from .models import Post


class MediaRssFeed(Rss201rev2Feed):
    """RSS 2.0 feed with media:content for images."""

    def rss_attributes(self):
        attrs = super().rss_attributes()
        attrs['xmlns:media'] = 'http://search.yahoo.com/mrss/'
        return attrs

    def add_item_elements(self, handler, item):
        super().add_item_elements(handler, item)
        if item.get('image_url'):
            handler.addQuickElement(
                'media:content',
                attrs={
                    'url': item['image_url'],
                    'medium': 'image',
                    'type': 'image/jpeg',
                }
            )


class LatestPostsFeed(Feed):
    feed_type = MediaRssFeed
    title = "PulseLineDaily"
    link = "/"
    description = "Latest breaking news, politics, sports and more from PulseLineDaily."

    def items(self):
        return Post.published.select_related('author').prefetch_related('categories').order_by('-date_created')[:20]

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

    def item_categories(self, item):
        return [cat.name for cat in item.categories.all()]

    def item_extra_kwargs(self, item):
        return {
            'image_url': item.image.url if item.image else None,
        }
