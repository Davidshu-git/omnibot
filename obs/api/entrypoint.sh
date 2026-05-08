#!/bin/sh
set -e
echo "[entrypoint] running alembic upgrade head..."
alembic upgrade head
echo "[entrypoint] starting uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
