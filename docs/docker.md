# Docker Installation and Usage

## Quick Start (Pre-built Image — Recommended)

Pre-built production images are published to GitHub Container Registry on every push to `master`.

```bash
docker run --pull always -it -p 5900:5900 -v openoutreach_db:/app/data ghcr.io/eracle/openoutreach:latest
```

The interactive onboarding will guide you through LinkedIn credentials, LLM API key, and campaign setup on first run. All data (CRM database, cookies, model blobs, embeddings) persists in the `openoutreach_db` Docker volume.

### Available Tags

| Tag | Description |
|:----|:------------|
| `latest` | Latest build from `master` |
| `sha-<commit>` | Pinned to a specific commit |
| `1.0.0` / `1.0` | Semantic version (when tagged) |

### VNC (Live Browser View)

The container includes a VNC server for watching the automation live. Connect any VNC client to `localhost:5900` (no password).

On Linux with `vinagre`:
```bash
vinagre vnc://127.0.0.1:5900
```

### Stopping & Restarting

```bash
# Find the container
docker ps

# Stop it
docker stop <container-id>

# Restart (data persists in the openoutreach_db volume)
docker run --pull always -it -p 5900:5900 -v openoutreach_db:/app/data ghcr.io/eracle/openoutreach:latest
```

---

## Build from Source (Docker Compose)

For development or customization, you can build the image locally. The compose file (`local.yml`)
mounts the entire project directory into the container for live code editing.

### Prerequisites

- [Make](https://www.gnu.org/software/make/)
- [Docker](https://www.docker.com/)
- [Docker Compose](https://docs.docker.com/compose/)

### Build & Run

```bash
git clone https://github.com/eracle/OpenOutreach.git
cd OpenOutreach

# Build and start
make up
```

This builds the Docker image from source with `BUILD_ENV=local` (includes test dependencies) and starts the daemon.

**Note:** The compose file uses `HOST_UID` / `HOST_GID` environment variables (defaulting to 1000)
for file ownership. If your host UID differs from 1000, set them explicitly:

```bash
HOST_UID=$(id -u) HOST_GID=$(id -g) make up
```

### Useful Commands

| Command | Description |
|:--------|:------------|
| `make build` | Build the Docker image without starting |
| `make up` | Build and start the service |
| `make stop` | Stop the running containers |
| `make logs` | Follow application logs |
| `make up-view` | Start + open VNC viewer (Linux, requires `vinagre`) |
| `make view` | Open VNC viewer standalone (requires `vinagre`) |
| `make docker-test` | Run the test suite in Docker |

### VNC with Docker Compose

The VNC server is exposed on port 5900. Use `make up-view` to auto-open it, or connect manually to `localhost:5900` with any VNC client.

### Volume Mounts

The pre-built `docker run` command uses a named Docker volume (`openoutreach_db`) mounted at `/app/data` for data persistence (database, config). The compose setup (`local.yml`) mounts the entire repo `.:/app` for live code editing during development.

## Release Runbook

There is no CI in this repo — `tests.yml` / `deploy.yml` / `docker.yml` were deleted in `b6326f3`. A release is built manually and pulled by each server.

```sh
# 1. Build and push. The registry is anonymously readable, so the servers
#    need no credentials of their own.
gcloud builds submit --project gen-lang-client-0289784019 --config cloudbuild.yaml .
```

Then, **one server at a time**:

```sh
# 2. Back up the database with the daemon stopped.
docker stop openoutreach
cp /var/lib/docker/volumes/openoutreach_openoutreach_db/_data/db.sqlite3 /root/backups/

# 3. Pull, then start the DAEMON first and watch the migration apply.
cd /home/conf/app/openoutreach
docker compose pull
docker compose up -d openoutreach
docker logs -f openoutreach | grep -i applying

# 4. Only then the admin.
docker compose up -d openoutreach-admin
```

Three things that will bite you:

- **Order matters.** Only `rundaemon` migrates (`call_command("migrate")` at startup). Start the admin first and it boots against a schema that does not yet know the states its changelist filters on — the queue page is then the first thing anyone sees fail.
- **Remove stale bind mounts.** Servers may bind-mount `*_patched.py` files over paths inside the image; that is how hot fixes were shipped before this runbook existed. Each one silently shadows the new image. Grep `docker-compose.yml` for `:/app/` mounts and drop every one whose content is already in `main` — `md5sum` against the repo file tells you which.
- **One server per day.** `_seed_deal_tasks` re-creates a follow_up for every CONNECTED deal at boot. After a long outage that is a burst of activity from an account that has been silent for weeks — exactly the pattern LinkedIn scores against you.

`cloudbuild.yaml` pushes a single mutable tag, so there is no rollback handle today; add a `:sha-$SHORT_SHA` tag alongside it if you need one.
