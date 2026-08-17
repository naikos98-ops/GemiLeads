#!/bin/bash
set -e

BACKUP_DIR="/var/lib/postgresql/backups"
DB_NAME=${POSTGRES_DB:-gemileads}
DB_USER=${POSTGRES_USER:-gemileads_user}
DATE=$(date +%Y-%m-%d_%H-%M-%S)

mkdir -p "$BACKUP_DIR"

echo "Starting backup for database $DB_NAME..."
pg_dump -U "$DB_USER" -d "$DB_NAME" -F c -f "$BACKUP_DIR/${DB_NAME}_${DATE}.dump"

# Keep only the last 7 days of backups
find "$BACKUP_DIR" -type f -name "*.dump" -mtime +7 -delete

echo "Backup completed successfully."
