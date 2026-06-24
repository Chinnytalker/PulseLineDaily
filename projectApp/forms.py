
from django import forms
from .models import Comment, ContactMessage, NewsletterSubscriber, Applicant, Author, Post


class AuthorForm(forms.ModelForm):
    class Meta:
        model = Author
        fields = ['name', 'bio', 'photo', 'email']


class CommentForm(forms.Form):
    author = forms.CharField(
        max_length=60,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "Your Name"}
        ),
    )
    body = forms.CharField(
        widget=forms.Textarea(
            attrs={"class": "form-control", "placeholder": "Leave a comment!"}
        )
    )


class SearchForm(forms.Form):
    query = forms.CharField(label="search", max_length=100)


class ContactForm(forms.ModelForm):
    class Meta:
        model = ContactMessage
        fields = ['name', 'email', 'subject', 'message']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Your Name'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'Your Email'}),
            'subject': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Subject'}),
            'message': forms.Textarea(attrs={'class': 'form-control', 'placeholder': 'Your Message'}),
        }


class NewsletterSubscriptionForm(forms.Form):
    email = forms.EmailField(widget=forms.EmailInput(attrs={
        'class': 'form-control',
        'placeholder': 'Enter your email'
    }))


class JobApplicationForm(forms.ModelForm):
    class Meta:
        model = Applicant
        fields = ['applicant_name', 'applicant_email', 'resume', 'cover_letter']


class AdvertisementRequestForm(forms.Form):
    """Collect ad request info and forward via email — not a direct model save."""
    advertiser_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Your name or company'})
    )
    advertiser_email = forms.EmailField(
        widget=forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'your@email.com'})
    )
    preferred_location = forms.ChoiceField(
        choices=[('header', 'Header'), ('sidebar', 'Sidebar'), ('footer', 'Footer')],
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    ad_url = forms.URLField(
        required=False,
        widget=forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://yourwebsite.com'})
    )
    message = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'placeholder': 'Tell us about your ad...', 'rows': 4})
    )


class DraftSubmissionForm(forms.ModelForm):
    """Public form for contributors to submit article drafts for editorial review."""
    contributor_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Your name (for attribution)'})
    )

    class Meta:
        model = Post
        fields = ['title', 'body', 'summary', 'tags', 'categories']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Article title'}),
            'body': forms.Textarea(attrs={
                'class': 'form-control',
                'placeholder': 'Write your article here...',
                'rows': 15,
            }),
            'summary': forms.Textarea(attrs={
                'class': 'form-control',
                'placeholder': 'Brief summary (optional)',
                'rows': 3,
            }),
            'tags': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. politics, nigeria, election',
            }),
            'categories': forms.CheckboxSelectMultiple(),
        }
