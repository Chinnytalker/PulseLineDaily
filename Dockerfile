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

# Install frontend dependencies inside the Linux image
COPY theme/static_src/package.json theme/static_src/package-lock.json /app/theme/static_src/
WORKDIR /app/theme/static_src
RUN npm ci

# Restore app workdir
WORKDIR /app

# Copy all project files
COPY . .

# Build frontend assets and collect static files inside the image
RUN rm -rf /app/theme/static/css/dist /app/staticfiles \
    && python manage.py tailwind build \
    && python manage.py collectstatic --noinput

# Run the app as a non-root user in production containers
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser


# Expose ports
EXPOSE 2001

# Start  Gunicorn
CMD ["gunicorn", "MaxDiscoverHub.wsgi:application", "--bind", "0.0.0.0:2001"]
