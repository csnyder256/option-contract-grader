FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/csnyder256/option-contract-grader"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DB_PATH=/opt/option/data/options.db
WORKDIR /opt/option
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY frontend frontend
RUN useradd --create-home --uid 10001 option && mkdir data && chown option:option data
USER option
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-server-header"]
