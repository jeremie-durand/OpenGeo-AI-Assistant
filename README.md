# OpenGeo AI Assistant (OpenGAA)

An open-source, AI-powered geospatial assistant for exploring, analyzing, and visualizing Earth data using natural language queries. This project is based on the original Earth Copilot by Microsoft, but is Azure-free.

## Project Description

OpenGeo AI Assistant enables users to:

- Search and visualize satellite and geospatial data collections with natural language
- Run locally via Docker, without any Azure dependencies
- Extend and customize backend and frontend modules for new data sources or workflows

The project is inspired by and reuses open source patterns from [Earth Copilot](https://github.com/microsoft/Earth-Copilot), but is not affiliated with or endorsed by Microsoft. See the credit section below for attribution.

## Differences from the Original Project

This project extends and adapts the original Earth Copilot codebase with the following changes:

- Removed all dependencies to Microsoft Azure tools:
  - Azure OpenAI -> LLM is now decided by the user with `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` environemnt variables
  - Azure Maps -> Leaflet and other open-source tools

- Implemented custom large language model (LLM) options instead of relying solely on OpenAI
- Implemented Open-Meteo API

## Quick Deploy

See [QUICK_DEPLOY.md](QUICK_DEPLOY.md)

## Security

See [SECURITY.md](SECURITY.md) for the vulnerability reporting policy.

**Production deployments must:**

1. Set `ENABLE_AUTH=true` and generate a strong API key (`openssl rand -hex 32`) — the default `ENABLE_AUTH=false` is for local development only
2. Set `CORS_ORIGINS` to your specific frontend domain — the default wildcard (`*`) must not be used in production

## Vector search

The assistant finds STAC collections by meaning, so a question in French or
English can reach any collection the map can render, including those of your own
`STAC_API_URL`. It works with no configuration: the backend image bundles a
multilingual embedding model and builds a Parquet index at startup. Set
`VECTOR_SEARCH_ENABLED=false` to turn it off. Every setting is documented in the
"Vector search" section of [`.env.example`](.env.example).

`VECTOR_SEARCH_URL` chooses where the vectors live:

| URL | Backend | Writes |
|---|---|---|
| empty, `file://...parquet` | Parquet file read with DuckDB | managed or external |
| `s3://...parquet`, `https://...parquet` | remote Parquet file | read-only |
| `postgresql://` | PostgreSQL with pgvector and PostGIS | managed or external |
| `mongodb://`, `mongodb+srv://` | MongoDB `$vectorSearch` | managed or external |
| other `http(s)://` | your own HTTP retriever | read-only |

In `managed` mode (the default) the assistant builds and refreshes its own index.
In `external` mode it only reads documents you maintain, which must follow the
contract below. `/api/health` shows the `vector_search` status (`ready`,
`building`, `degraded` or `disabled`) without affecting the overall health.

The index builds in the background at startup and refreshes every 24 hours. To
build it, export it or check it by hand:

```bash
docker compose run --rm backend python -m services.vector_search.cli build
docker compose run --rm backend python -m services.vector_search.cli export --out /app/data/index.parquet
docker compose run --rm backend python -m services.vector_search.cli status
```

`build` prints how many documents were added, updated, unchanged and removed, and a
second run with no catalog change re-embeds nothing. An exported file follows the
Parquet contract below and can be served with `VECTOR_SEARCH_URL=file://...` and
`VECTOR_SEARCH_MODE=external`.

### Dataset search API

`POST /api/datasets/search` finds collections by meaning without calling the LLM.
It is rate limited like the other search routes (`RATE_LIMIT_SEARCH`) and needs
the `X-Api-Key` header when `ENABLE_AUTH=true`.

```bash
curl -X POST http://localhost:8000/api/datasets/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "intensité des feux de forêt", "k": 5,
       "bbox": [-125.0, 24.0, -66.0, 50.0], "datetime": "2020-01-01/.."}'
```

```json
{"results": [
  {"id": "mtbs", "title": "MTBS: Monitoring Trends in Burn Severity",
   "score": 0.78, "renderable": true, "source": "planetary_computer"}
]}
```

- `query`: 1 to 500 characters (required); `k`: 1 to 50, default 10.
- `bbox`: `[west, south, east, north]` in EPSG:4326, with west < east (split a
  search across the antimeridian into two requests); only collections whose extent
  intersects it are returned.
- `datetime`: an RFC 3339 instant or interval, `..` for an open end; only
  collections whose time range overlaps it are returned.
- `renderable` says whether the map can display the collection; `source` is
  `planetary_computer`, `private_stac` or `external`.
- Invalid input returns 422. While vector search is disabled, building or degraded,
  the endpoint returns 503 with the reason; when the backend fails during a
  request it returns 503 with a generic message and logs the details.

### Parquet contract

| Column | Type | Notes |
|---|---|---|
| `id` | string | STAC collection id |
| `title`, `description` | string | |
| `embedding` | list of float | same model and dimension as `EMBEDDING_MODEL` |
| `minx`, `miny`, `maxx`, `maxy` | double | extent in EPSG:4326; all null means global; `minx > maxx` crosses the antimeridian |
| `start_datetime`, `end_datetime` | timestamp with time zone | null means open-ended |

Managed files also hold `model`, `content_hash` and `source`. Scores are
`(1 + cosine similarity) / 2`, from 0 to 1. For `s3://` files, credentials come
from the standard `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN`, `AWS_REGION` and `AWS_ENDPOINT_URL` variables.

### PostgreSQL

The database needs the `vector` (pgvector) and `postgis` extensions. In managed
mode the assistant creates them if it is allowed to, then creates and fills its own
table, `VECTOR_SEARCH_TABLE` (default `opengeo.collection_embeddings`), and touches
nothing else. If the database user cannot create extensions or schemas, have an
administrator run this once:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS opengeo AUTHORIZATION <assistant_user>;
```

In external mode the assistant only reads a view (or table) named
`opengeo_search_collections`, for example:

```sql
CREATE VIEW opengeo_search_collections AS
SELECT collection_id AS id,
       title,
       description,
       embedding::vector(384) AS embedding,   -- same model as EMBEDDING_MODEL
       footprint AS geom,                     -- geometry in EPSG:4326, NULL = global
       start_date AS start_datetime,          -- timestamptz, NULL = open
       end_date AS end_datetime
FROM my_catalog;
```

At startup the assistant checks the view's columns and compares the vector
dimension with the embedding model, and reports vector search as degraded in
`/api/health` if they don't match.

### MongoDB

`$vectorSearch` needs MongoDB Atlas, or MongoDB Community 8.2+ with `mongot`
(`mongodb/mongodb-atlas-local` works for local testing). The database is the one in
the URL path (`opengeo` when there is none), the collection is
`VECTOR_SEARCH_COLLECTION` and the vector search index is
`VECTOR_SEARCH_MONGO_INDEX`. In managed mode the assistant creates the index and
waits until it is queryable.

In external mode every document must carry these fields:

| Field | Type | Notes |
|---|---|---|
| `id`, `title`, `description` | string | |
| `embedding` | array of numbers | same model and dimension as `EMBEDDING_MODEL` |
| `minx`, `miny`, `maxx`, `maxy` | number | always present; global is `-180, -90, 180, 90`; an extent crossing the antimeridian is stored as `-180` to `180` in longitude |
| `start_datetime`, `end_datetime` | date | always present; open ends are `0001-01-01T00:00:00Z` and `9999-12-31T23:59:59Z` |

Example index definition (`type: "vectorSearch"`):

```json
{
  "fields": [
    {"type": "vector", "path": "embedding", "numDimensions": 384, "similarity": "cosine"},
    {"type": "filter", "path": "minx"},
    {"type": "filter", "path": "miny"},
    {"type": "filter", "path": "maxx"},
    {"type": "filter", "path": "maxy"},
    {"type": "filter", "path": "start_datetime"},
    {"type": "filter", "path": "end_datetime"}
  ]
}
```

### Your own HTTP retriever

Point `VECTOR_SEARCH_URL` at any `http(s)://` endpoint that is not a `.parquet` file
to let your own search service rank collections. No embedding model is loaded:
your service receives the text. The assistant sends:

```http
POST <VECTOR_SEARCH_URL>
Authorization: Bearer <VECTOR_SEARCH_API_KEY>    (only when set)
Content-Type: application/json

{"query": "burn severity", "k": 10, "kind": "collections",
 "bbox": [-79.8, 45.0, -57.1, 62.6], "datetime": "2001-01-01/2001-12-31"}
```

`bbox` and `datetime` are only present when the caller filters. Answer with
results ordered best first, where a higher `score` means more relevant (scores are
passed on unchanged):

```json
{"results": [{"id": "mtbs", "score": 0.82, "title": "MTBS burn severity"}]}
```

A non-2xx status, an invalid body or no answer within
`VECTOR_SEARCH_TIMEOUT_SECONDS` counts as unavailable, and the assistant falls back
to its previous behaviour. A minimal service:

```python
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class Search(BaseModel):
    query: str
    k: int = 10
    kind: str = "collections"
    bbox: list[float] | None = None
    datetime: str | None = None


@app.post("/search")
def search(request: Search) -> dict:
    hits = my_ranker(request.query, request.k, request.bbox, request.datetime)
    return {"results": [{"id": h.id, "score": h.score, "title": h.title} for h in hits]}
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md)

## Future Features

Semantic (vector-based) search over STAC collections is now built in; see
[Vector search](#vector-search).

The `opengeo-ai-assistant/mcp-server` directory contains code and configuration for an optional Model Context Protocol (MCP) server. This server is designed for advanced AI/LLM context management, agent orchestration, and integration with external tools or workflows. It is not required for basic usage or local deployments, but can be used if you need multi-agent coordination, persistent conversation context, or a backend bridge for complex AI workflows.

## License

See [LICENSE.txt](LICENSE.txt). Third-party material bundled in the Docker images,
such as the vector search embedding model, is listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Credits & Acknowledgments

This project is based on the open-source Earth Copilot project originally published by Microsoft.

For more information, see the official presentation:
[Satya Nadella introduces NASA Earth Copilot 1.0 at Microsoft Ignite 2024](https://www.linkedin.com/posts/microsoft_msignite-activity-7265061510635241472-CAYx/?utm_source=share&utm_medium=member_desktop)

The code, architecture, and documentation are inspired by the initial open-source repository. Thanks to all contributors of the original project. [See original repository](https://github.com/microsoft/Earth-Copilot/tree/main)

Earth Copilot was developed by Melisa Bardhi and advised by Juan Carlos Lopez.

The original Earth Copilot project involved contributions and collaboration from:

- Microsoft Planetary Computer  
- NASA  
- Microsoft team members including Juan Carlos Lopez, Jocelynn Hartwig, Minh Nguyen, and Matt Morrell.

This project is an independent work and is not affiliated with, endorsed by, or sponsored by Microsoft or NASA.
