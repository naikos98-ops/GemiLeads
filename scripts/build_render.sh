#!/usr/bin/env bash
# exit on error
set -o errexit

echo "Installing requirements..."
pip install --upgrade pip
pip install -r requirements.txt

echo "Building the Tailwind stylesheet..."
npm ci --no-audit --no-fund
npm run build:css

echo "Collecting static files..."
python manage.py collectstatic --noinput
