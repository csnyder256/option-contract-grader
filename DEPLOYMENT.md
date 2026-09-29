# Install and update Option Contract Grader

## Container install

Download the `v0.2.1` deployment bundle from [Releases](https://github.com/csnyder256/option-contract-grader/releases), verify its SHA-256 against `checksums.txt`, and extract it. From that folder:

```sh
docker compose -p option-grader up --build -d
```

Open <http://localhost:8000>; <http://localhost:8000/health> verifies startup. Python dependencies install during the image build. The process runs as an unprivileged user. The named `grader-data` volume retains the SQLite database across rebuilds. `GRADER_PORT` changes the local port.

Set `TRADIER_TOKEN` in your shell or a private `.env` beside `compose.yml` only if you use Tradier. Credentials enter at runtime and are excluded from image builds and release bundles. See README for provider modes and their data limitations. The interface has no login and binds to loopback; use it on a trusted machine.

## Python install

The deployment bundle also contains the complete application. Requires Python 3.11+:

```sh
python -m venv .venv
# Windows: use .venv\Scripts\python.exe
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app.api:app --host 127.0.0.1 --port 8000
```

Create a `data` folder before scanning if it does not exist. The container creates it for you. Configure optional credentials as described in README.

## Upgrade and rollback

Stop the app before copying its database. Export the named volume with Docker Desktop or archive it using a temporary utility container. Keep a matching backup and the previous release folder. Extract updates to a new folder, retain private environment settings, and keep `-p option-grader` so Compose reuses the data volume. Run `up --build -d`; confirm `/health` and a saved board before deleting the previous folder. For Python installs, move `data` while stopped and use a new virtual environment.
