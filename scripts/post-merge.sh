#!/bin/bash
set -e

uv sync

echo ""
echo "Running route health check..."
uv run python scripts/smoke_test_routes.py
