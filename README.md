# PulselineDaily

**PulselineDaily** is a Django-based news website that delivers breaking news, summaries, and in-depth stories from around the world. The platform fetches news from APIs and allows manual content posting through the Django admin. Built for scalability and user engagement, PulselineDaily combines backend efficiency with a clean, responsive frontend using Tailwind CSS and vanilla JavaScript.

---

## Features

- Fetch and display news articles from external APIs
- Automatic summarization of titles and descriptions using Hugging Face models
- Responsive user interface built with Tailwind CSS
- Full Django admin support for manual article creation
- Dynamic dashboards and categorization for content organization
- Image and link storage for news articles
- Secure user authentication (optional, if applicable)

---

## Technologies Used

- **Backend:** Python, Django
- **Frontend:** HTML, Tailwind CSS, JavaScript
- **Database:** PostgreSQL / SQLite
- **APIs:** External news sources
- **AI Integration:** Hugging Face `facebook/bart-large-cnn` for summaries

---

## Installation / Setup

1. **Clone the repository**

```bash
git clone https://github.com/yourusername/pulselinedaily.git
cd pulselinedaily
```
2. **Create virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/venv/activate # on Windows: venv\Scripts\activate
   ``` 
3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```
   
4. **Apply migrations**
   ```bash
   python manage.py migrate
   ```
5. **Create superuser**
   ```bash
   python manage.py createsuperuser
   ```
6. **Run server**
   ```bash
   python manage.py runserver
   ```
   Visit http://127.0.0.1:8000/ in your browser to see the site.

---


## Usage
- Admin users can log in Via /admin to create or manage news posts manually.
- The system can fetch News from configured APIs automatically
- Summarized content is displayed on the homepage for a quick read

---

## contributing
- Fork the repository
- create new  branch (git checkout -b feature)
- commit your changes (git commit -m 'Add some features')
- push to the branch (git push origin feature)
- Open a Pull Request

---
## License
This project is open-source and available under the MIT License

