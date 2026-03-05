# Quick Startup Guide

Prerequisites and step-by-step instructions for running LocalGenBI-Agent in both deployment modes.

---

## Prerequisites

### Both modes

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | 3.10 may work but is untested |
| PostgreSQL | 15+ | Must be accessible from application hosts |
| Ollama | Latest | [ollama.ai](https://ollama.ai) |
| Llama 3 model | 8b |` ollama pull llama3:8b` |

### Docker mode only

| Requirement | Notes |
|---|---|
| Docker Desktop or Docker Engine | 24.0+ |
| Docker Compose plugin | v2 (`docker compose`, not `docker-compose`) |
| 12 GB free RAM | Ollama + 4× Postgres + 4× gateway + backend + frontend |
| 8 GB free disk | Model weights (~5 GB) + Docker images (~3 GB) |

---

## Option A — Docker Compose (Recommended)

Starts all 11 services in the correct dependency order.

### Step 1 — Configure environment

```bash
cp .env.docker .env
```

Open `.env` and fill in every value marked `← REQUIRED`:

```
DB_HEALTH_PASSWORD=your_health_db_password
DB_FINANCE_PASSWORD=your_finance_db_password
DB_SALES_PASSWORD=your_sales_db_password
DB_IOT_PASSWORD=your_iot_db_password
DB_ADMIN_PASSWORD=your_postgres_superuser_password
```

All other values have sensible defaults. If you change any port value, update both `.env` and the corresponding `ports:` mapping in `docker-compose.yml`.

### Step 2 — Build and start

```bash
docker compose up -d
```

First run builds Docker images and pulls the Postgres and Ollama base images (5–10 minutes).

### Step 3 — Pull the LLM model

```bash
docker exec localgenbi-ollama ollama pull llama3:8b
```

Approximately 5 GB. Only needs to be done once — the model is stored in the `localgenbi_ollama_models` Docker volume and survives `docker compose down` / `up` cycles.

### Step 4 — Initialise database schemas (once)

```bash
docker exec localgenbi-backend python setup_dbs.py
```

Creates all tables and the read-only application user across all four databases.

### Step 5 — Load demo data (optional)

```bash
docker exec localgenbi-backend python create_demo_data.py
```

Populates all databases with synthetic data: 100 patients / 500 claims, 1,000 transactions / 200 subscriptions, 300 leads / 150 opportunities, 18,250 IoT records (50 users × 365 days).

### Step 6 — Verify all services are healthy

```bash
docker compose ps
```

All 11 services should show `healthy`. If any show `unhealthy`, check logs:

```bash
docker compose logs <service-name>
```

### Step 7 — Open the UI

```
http://localhost:8000
```

---

## Option B — Native Python (Development)

Runs all services directly on your machine. Use this for hot-reload during development.

### Step 1 — Install Ollama and pull model

```bash
# Install from https://ollama.ai
ollama serve    # keep this running in a terminal
ollama pull llama3:8b
```

### Step 2 — Set up PostgreSQL

Create four databases on your local PostgreSQL instance:

```sql
CREATE DATABASE health_db;
CREATE DATABASE finance_db;
CREATE DATABASE sales_db;
CREATE DATABASE iot_db;
```

### Step 3 — Configure environment

```bash
cp .env.local .env
```

Fill in the required passwords. Default local settings assume:
- `DB_*_HOST=localhost`
- `DB_*_PORT=5432`
- `DB_ADMIN_USER=postgres`

### Step 4 — Install Python dependencies

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Step 5 — Initialise schemas and demo data

```bash
python setup_dbs.py
python create_demo_data.py
```

### Step 6 — Start DB gateway servers (4 terminals)

Each gateway is a separate process. Open four terminal windows from the project root with the virtualenv active:

```bash
# Terminal 1
python -m db_gateway.gateway_factory health

# Terminal 2
python -m db_gateway.gateway_factory finance

# Terminal 3
python -m db_gateway.gateway_factory sales

# Terminal 4
python -m db_gateway.gateway_factory iot
```

Verify each gateway is healthy:

```bash
curl http://localhost:3001/health  # → {"status":"healthy","domain":"health",...}
curl http://localhost:3002/health
curl http://localhost:3003/health
curl http://localhost:3004/health
```

### Step 7 — Start the FastAPI backend

```bash
# Terminal 5
uvicorn backend.main:app --host 0.0.0.0 --port 8001 --reload
```

Verify:
```bash
curl http://localhost:8001/health
```

### Step 8 — Start the Chainlit frontend

```bash
# Terminal 6
chainlit run frontend/app.py --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`.

---

## Port Reference

| Service | Default Port | Env var to change |
|---|---|---|
| Chainlit (UI) | 8000 | `CHAINLIT_PORT` |
| FastAPI (backend API) | 8001 | `FASTAPI_PORT` |
| DB gateway — health | 3001 | `GATEWAY_HEALTH_PORT` |
| DB gateway — finance | 3002 | `GATEWAY_FINANCE_PORT` |
| DB gateway — sales | 3003 | `GATEWAY_SALES_PORT` |
| DB gateway — iot | 3004 | `GATEWAY_IOT_PORT` |
| Ollama | 11434 | Ollama config |
| PostgreSQL (each) | 5432 | `DB_*_PORT` |

---

## Common Issues

**`ImportError: No module named 'db_gateway'`**
Run all commands from the project root directory, not from inside a subdirectory.

**Gateway health check fails on startup**
The gateway waits for its PostgreSQL instance to be ready. In Docker Compose this is handled by `depends_on: condition: service_healthy`. In native mode, ensure `setup_dbs.py` has run successfully before starting the gateways.

**`model "llama3:8b" not found`**
Run `ollama pull llama3:8b` (local) or `docker exec localgenbi-ollama ollama pull llama3:8b` (Docker).

**CORS error in browser**
`CHAINLIT_ALLOW_ORIGINS` must include the URL you open in your browser (protocol + port). The default `http://localhost:8000` works when accessing from localhost on port 8000.

**Slow first response**
The first query after startup loads Llama 3 into memory. On CPU-only hardware this can take 30–120 seconds. Subsequent queries are faster. Monitor backend logs to confirm inference is progressing.

**`ValidationError: db_ssl_mode — SSL must not be disabled in production`**
Your `.env` has `ENVIRONMENT=production` with `DB_SSL_MODE=disable` (or no `DB_SSL_MODE` set). Either use `ENVIRONMENT=development` for local testing, or configure Postgres SSL and set `DB_SSL_MODE=require`.