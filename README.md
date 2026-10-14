# MyPlayground

## Run the dashboard

```bash
python3 -m venv .venv
.venv/bin/pip install -r control/requirements.txt
CATALOG_PATH="$PWD/catalog.yaml" \
CATALOG_SCHEMA_PATH="$PWD/catalog.schema.json" \
SRC_DIR=/tmp/playground-src \
PUBLISH_HOST=127.0.0.1 \
PUBLIC_SCHEME=http \
ADMIN_TOKEN=local-dev \
PYTHONPATH=control \
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 and paste `local-dev` into the admin token field. Launch publishes each game on the `hostPort` listed in `catalog.yaml`.
