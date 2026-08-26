# Portable mode

Portable mode runs the panel, bots and scheduler as one foreground process. It
does not use root, apt, systemd, nginx or Docker. The runtime remains available
only while its host keeps the process alive; a Codex/sandbox session is not a
24/7 hosting guarantee.

## Start

```sh
cp .env.example .env
python3 -m venv .venv
.venv/bin/pip install -r requirements-portable.txt
PYTHON_BIN=.venv/bin/python ./start-portable.sh
```

Python 3.10 or newer is required; the portable container uses Python 3.12.

Open `http://127.0.0.1:8000/login`. On the first start the generated admin
password is printed once and stored in `data/.credentials.env` with mode 0600.
Set `PERSISTENT_STORAGE=1` only when `DATA_DIR` is backed by persistent storage.

All configuration is optional for a basic panel. `HOST`, `PORT`, `PUBLIC_URL`,
`DATA_DIR`, `MODE`, and `LOG_LEVEL` may be set through the environment. Telegram
works through polling and can be configured in the panel. WhatsApp requires an
HTTPS `PUBLIC_URL` and webhook mode. With no model key the panel still starts,
but the agent does not invent answers.

## Checks and maintenance

```sh
PYTHON_BIN=.venv/bin/python ./check-portable.sh
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
.venv/bin/python -m app.cli backup
.venv/bin/python -m app.cli check-db
.venv/bin/python -m app.cli reset-password
.venv/bin/python -m app.cli restore data-YYYYMMDD-HHMMSS.db
```

The database uses WAL and a busy timeout. Backups are placed under
`data/backups`. Stop the process with SIGTERM/SIGINT; background tasks and the
database connection are closed before exit.

For real 24/7 operation use the same command on a persistent PaaS, container
host, VPS, or server and configure process restart plus a persistent `DATA_DIR`.
The original `deploy/install.sh` remains available for a systemd-based VPS.
