# FastAPI Text Analyzer

A Dockerized FastAPI example with background jobs, live progress, and a persistent SQLite audit trail. Each word takes one second to process to simulate a long report.

## Run

```sh
git clone https://github.com/s4lt3d/FastAPI-Example.git
cd FastAPI-Example
docker compose up -d --build
```

The supplied Compose file binds to the example Pi's LAN address, `192.168.1.31:8000`. On another machine, change the port mapping in `compose.yaml`, for example to `127.0.0.1:8000:8000` for local access. Prefix Docker commands with `sudo` if your account requires it.

- Analyzer: http://192.168.1.31:8000/
- History: http://192.168.1.31:8000/history
- API docs: http://192.168.1.31:8000/docs

```sh
docker compose logs -f api
docker compose stop
docker compose up -d --build
```

## Using the app

Enter text, choose a reading speed, and optionally include word frequencies. Submitting opens a run page with a progress bar, timestamped progress log, and final output. Closing the browser does not stop processing. Reopening a run replays saved updates and continues receiving live updates.

History supports input/output search, status filters, UTC date ranges, and pagination. Click a timestamp for full inputs, options, output or errors, and technical details. **Run again** loads previous inputs for review; submitting creates a new audit entry.

## API

| Endpoint | Behavior |
| --- | --- |
| `POST /analyze` | Starts a job; returns HTTP 202 with `run_id` and `status` |
| `GET /active-run` | Returns the active `run_id`, or null |
| `GET /runs` | Lists records with `total`, `limit`, and `offset` |
| `GET /runs/{run_id}` | Returns inputs, progress or final output, status, timestamp, and duration |
| `GET /runs/{run_id}/events` | Streams saved and live progress as Server-Sent Events |
| `GET /health` | Checks database access |

```sh
curl http://192.168.1.31:8000/analyze \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello Pi! Hello Docker.","words_per_minute":200,"include_word_frequency":true}'

# Substitute the returned run ID:
curl -N http://192.168.1.31:8000/runs/RUN_ID/events
curl http://192.168.1.31:8000/runs/RUN_ID
```

Inputs: `text` (1-20,000 characters), `words_per_minute` (integer 1-2,000; default 200), and `include_word_frequency` (default false). Reading speed affects estimated reading time; the simulated delay remains one second per word. Words use Unicode word tokens with internal apostrophes or hyphens. Character count includes whitespace and punctuation.

`GET /runs` accepts `q`, `status`, `from`, `to`, `limit` (1-100), and `offset`. Dates use `YYYY-MM-DD`; both dates are included in the UTC range. States: `running`, `success`, `validation_error`, `error`, and `interrupted`.

SSE updates have an event ID and JSON containing sequence, timestamp, message, status, processed count, and total count when available. Resume using `Last-Event-ID` or `?after=SEQUENCE`. A `done` event ends the stream for terminal runs.

## Concurrency and restarts

A SQLite transaction and unique partial index enforce one active job. Another submission receives HTTP 409 and the existing run ID; the browser opens that run. This prevents simultaneous execution rather than deduplicating sequential submissions.

Keep **one app worker and one container per database**, as configured. Graceful shutdown or startup recovery marks unfinished work `interrupted`; jobs do not resume automatically. Audit write failures prevent successful results. If failure recording also fails, the active lock remains until restart.

## Storage and logs

SQLite lives at `/data/audit.sqlite3` in the named `audit-data` volume. Records and progress events survive container replacement. Do not use `docker compose down --volumes` unless you intend to delete history. Use SQLite's backup API for a consistent backup of a running database.

Audit records retain UTC timestamps, inputs, outputs, status, and duration. Validation-error duration measures handler time only. Progress messages and final outputs also go to stdout. Docker rotates logs at 10 MB with three files; database history is retained indefinitely.

This example has no authentication or TLS. Anyone who can reach the API can submit work and read history. Audit entries are not tamper-resistant. Abrupt crashes before commit cannot guarantee a record.

## Verify

With the service running and no other active job:

```sh
python3 verify.py
```

Standard-library checks cover six simultaneous submissions, responsiveness, per-word progress, final output, SSE replay/resume, audit retrieval, lock release, and validation. They create audit records in the running service. Set `API_BASE_URL` to test another host:

```sh
API_BASE_URL=http://127.0.0.1:8000 python3 verify.py
```

The original Pi deployment lives at `/home/pi2/text-audit-api`.
