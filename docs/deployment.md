# Deployment: one server with Docker Compose

The target is one small Linux VM (2 vCPU, 4 GB RAM, about 20 GB disk) with Docker installed, plus a DNS A record (for example `api.yourdomain.com`) pointing at it. Any VPS works, such as Hetzner, DigitalOcean or Lightsail. Caddy obtains and renews the HTTPS certificate automatically.

## First deployment

```bash
# On the server
git clone <backend repo> SemProject_Backend
git clone <ML engine repo> SemProject_MLEngine   # the ml image is built from it
cd SemProject_Backend
cp .env.example .env
#   set API_DOMAIN, POSTGRES_PASSWORD (in DATABASE_URL too), JWT_SECRET (openssl rand -hex 32),
#   ML_SERVICE_TOKEN (openssl rand -hex 24)
docker compose up -d --build
docker compose logs -f api            # migrations run on start; wait for "Application startup complete"
curl https://$API_DOMAIN/healthz
```

Open only ports 22, 80 and 443 in the firewall. Postgres and the ML service publish no ports.

## Load the demo workspace

The demo data comes from the ML engine's export (about 100 MB, which needs the raw M5 CSVs). Produce it on your machine and copy it to the server:

```bash
# Locally
(cd ../SemProject_MLEngine && python -m mlengine export-reference)
scp -r ../SemProject_MLEngine/data/processed/reference server:SemProject_Backend/seed-data/

# On the server (the api image has the CLI)
docker compose run --rm -v "$PWD/seed-data:/seed:ro" api python -m app.cli seed-reference /seed
docker compose run --rm api python -m app.cli create-user you@example.com --research
```

Seeding is idempotent: running it again replaces the demo store's data.

## Updates

```bash
git pull && (cd ../SemProject_MLEngine && git pull)
docker compose up -d --build          # api runs `alembic upgrade head` on start
```

**New model versions:** the `models` volume keeps what was copied on first start. After an ML release, copy the new version folder into the volume:

```bash
docker compose cp ../SemProject_MLEngine/models/m5-ca1-foods/1.2.0 ml:/app/models/m5-ca1-foods/
docker compose restart ml
```

## Backups

```bash
docker compose exec -T db pg_dump -U forecast -Fc forecast > backup_$(date +%F).dump   # nightly via cron
docker run --rm -v semproject_backend_models:/m -v "$PWD":/b alpine tar czf /b/models_$(date +%F).tgz -C /m .
```

Restore a database dump with `pg_restore -U forecast -d forecast --clean`.

## Operations

| Task | Command |
| --- | --- |
| Logs | `docker compose logs -f api worker ml` |
| More forecast throughput | `docker compose up -d --scale worker=3` (claims are safe to run in parallel) |
| Database shell | `docker compose exec db psql -U forecast` |
| Stuck jobs | The worker requeues runs stuck for more than 5 minutes, hourly |
| Turn off public sign-up | `ALLOW_SELF_REGISTRATION=false` in `.env`, then `docker compose up -d`; create users with the CLI |

Local development without Caddy: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build` serves the API on `http://localhost:8000`.
