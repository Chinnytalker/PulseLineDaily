FROM python:3.12-slim

# Environment
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    curl \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt

# Install frontend dependencies
COPY theme/static_src/package.json theme/static_src/package-lock.json /app/theme/static_src/
WORKDIR /app/theme/static_src
RUN npm ci

# Back to app root
WORKDIR /app

# Copy project
COPY . .

# ONLY remove old build (safe)
RUN rm -rf /app/theme/static/css/dist

# ❌ DO NOT run Django here
# RUN python manage.py tailwind build   <-- removed

# Create non-root user
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 2001

CMD ["gunicorn", "MaxDiscoverHub.wsgi:application", "--bind", "0.0.0.0:2001", "--workers", "3", "--timeout", "120"]