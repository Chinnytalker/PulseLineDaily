
from django import forms
from .models import Advertisement, Comment, ContactMessage, NewsletterSubscriber, Applicant, Author

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
        fields = ['applicant_name', 'applicant_email', "resume", 'cover_letter', 'role']

    
        
        
class AdvertisementRequestForm(forms.ModelForm):
    advertiser_name = forms.CharField(max_length=150)
    advertiser_email = forms.EmailField()
    preferred_location = forms.ChoiceField(choices=Advertisement._meta.get_field('location').choices)
    ad_image = forms.ImageField(required=False)
    ad_url = forms.URLField(required=False)
    message = forms.CharField(widget=forms.Textarea, required=False)
    
    class Meta:
        model = Advertisement
        fields = ['advertiser_name', 'advertiser_email', 'preferred_location', 'ad_image', 'ad_url', 'message']

    def __str__(self):
        return self.advertiser_name