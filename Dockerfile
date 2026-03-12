# Use official Python image as base
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set work directory
WORKDIR /app

# Install system dependencies + Node.js (for Tailwind)
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    nginx \
    curl \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt


# Copy all project files
COPY . .


# Collect Django static files
RUN python manage.py collectstatic --noinput || true


# Expose ports
EXPOSE 2001

# Start  Gunicorn
CMD ["gunicorn", "MaxDiscoverHub.wsgi:application", "--bind", "0.0.0.0:2001"]