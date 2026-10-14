FROM python:3.12.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NUTRIOMICS_PUBLIC_RELEASE=/dataset
WORKDIR /app
COPY pyproject.toml uv.lock LICENSE LICENSE-DATA.md ./
COPY nutrigenomics ./nutrigenomics
COPY frontend/explorer/dist ./frontend/explorer/dist
COPY config/explorer_export_rights.json ./config/explorer_export_rights.json
# Exact dependencies are resolved by the repository lock, not a new pip solve.
RUN pip install --no-cache-dir uv==0.8.22 && uv sync --frozen --no-dev
USER 65532:65532
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "nutrigenomics.public_service:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000"]
