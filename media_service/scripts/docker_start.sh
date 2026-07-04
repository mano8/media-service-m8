#!/bin/sh
set -e

# Run migrations
# Check if alembic/versions has no .py files (ignores .gitkeep)
MIGRATION_DIR=/opt/shared_migrations/media/versions
LEGACY_MIGRATION_DIR=/opt/shared_migrations/m8_app/versions
mkdir -p "$MIGRATION_DIR"
if [ -z "$(find "$MIGRATION_DIR" -maxdepth 1 -name '*.py' -print -quit)" ] && [ -d "$LEGACY_MIGRATION_DIR" ]; then
    for revision in "$LEGACY_MIGRATION_DIR"/*.py; do
        [ -f "$revision" ] || continue
        if grep -q "media_service" "$revision"; then
            cp "$revision" "$MIGRATION_DIR/"
        fi
    done
fi
if [ -z "$(find "$MIGRATION_DIR" -maxdepth 1 -name '*.py' -print -quit)" ]; then
    echo "Generating Alembic migration..."
    if ! alembic -c /opt/media_service/alembic.ini revision --autogenerate -m "Initial media migration"; then
        echo "Failed to generate initial migration"
        exit 1
    else
        echo "Initial migration generated..."
    fi
else
    echo "Migrations already exist, skipping generation."
fi

# Run any pre-start tasks
echo "Initialyse DB and data..."
echo "Checking if pre_start.sh exists at $(pwd)/media_service/scripts/pre_start.sh"
ls -l $(pwd)/media_service/scripts/pre_start.sh
if ! ./media_service/scripts/pre_start.sh; then
    echo "Failed to initialise DB and data"
    exit 1  # Ensure the script exits if needed
fi

# Start the FastAPI server
if [ "$VSCODE_DEBUG" = "true" ]; then
  echo "Starting media_service under VS Code debugpy..."
  exec python -m debugpy \
    --listen 0.0.0.0:5678 \
    --wait-for-client \
    -m uvicorn media_service.main:app \
      --host 0.0.0.0 --port 8000 --reload
else
  exec uvicorn media_service.main:app --host 0.0.0.0 --port 8000
fi
