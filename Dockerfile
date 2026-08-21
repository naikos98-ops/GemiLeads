FROM python:3.12-slim

# Prevent Python from writing .pyc files
ENV PYTHONDONTWRITEBYTECODE 1
# Ensure stdout/stderr are unbuffered
ENV PYTHONUNBUFFERED 1

# Set work directory
WORKDIR /app

# Install system dependencies (for psycopg binary and other potential needs)
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Build the Tailwind stylesheet before collectstatic, otherwise the image ships without styles.
COPY package.json package-lock.json /app/
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm     && npm ci --no-audit --no-fund     && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY . /app/

RUN npm run build:css

# Make entrypoint executable
RUN chmod +x /app/scripts/entrypoint.sh

# Collect static files
RUN python manage.py collectstatic --noinput

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
