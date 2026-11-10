import asyncio, json, logging, os, re, sqlite3, time, uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

DB = Path(os.getenv('AUDIT_DB', '/data/audit.sqlite3'))
logging.basicConfig(level=logging.INFO, format='%(message)s')
log = logging.getLogger('audit')

@contextmanager
def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()

@asynccontextmanager
async def lifespan(app):
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, inputs TEXT NOT NULL, output TEXT NOT NULL, duration_ms REAL NOT NULL, status TEXT NOT NULL)')
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id), timestamp TEXT NOT NULL, payload TEXT NOT NULL)')
        db.execute('CREATE INDEX IF NOT EXISTS events_run ON events(run_id, sequence)')
        for row in db.execute("SELECT id FROM runs WHERE status='running'").fetchall():
            payload = {'message': 'Service restarted before completion', 'status': 'interrupted'}
            db.execute("UPDATE runs SET status='interrupted', output=? WHERE id=?", (json.dumps({'error': payload['message']}), row['id']))
            db.execute('INSERT INTO events(run_id,timestamp,payload) VALUES(?,?,?)', (row['id'], now(), json.dumps(payload)))
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_running_job ON runs(status) WHERE status='running'")
    app.state.stop = threading.Event()
    app.state.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='analysis')
    yield
    app.state.stop.set()
    app.state.worker.shutdown(wait=True)

app = FastAPI(title='Text Analysis & Audit API', lifespan=lifespan)

class AnalysisInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=20000)
    words_per_minute: int = Field(default=200, ge=1, le=2000, strict=True)
    include_word_frequency: bool = False

def analyze(args):
    words = re.findall(r"\b\w+(?:['’-]\w+)*\b", args.text.lower())
    result = {'word_count': len(words), 'character_count': len(args.text), 'reading_time_seconds': round(len(words) / args.words_per_minute * 60, 2)}
    if args.include_word_frequency:
        result['word_frequency'] = dict(Counter(words).most_common())
    return result

def record(inputs, output, status, started):
    run = dict(id=str(uuid.uuid4()), timestamp=datetime.now(timezone.utc).isoformat(), inputs=jsonable_encoder(inputs), output=jsonable_encoder(output), duration_ms=round((time.perf_counter()-started)*1000, 3), status=status)
    try:
        with connect() as db:
            db.execute('INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)', (run['id'], run['timestamp'], json.dumps(run['inputs']), json.dumps(run['output']), run['duration_ms'], status))
    except sqlite3.Error:
        log.exception('Audit write failed')
        raise HTTPException(503, 'Audit unavailable; request cannot be reported as successful')
    log.info(json.dumps(dict(event='analysis_run', **run), ensure_ascii=False))
    return run

@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    errors = jsonable_encoder(exc.errors(), custom_encoder={ValueError: str})
    if request.url.path != '/analyze':
        return JSONResponse(status_code=422, content={'detail': errors})
    run = record(exc.body, {'detail': errors}, 'validation_error', time.perf_counter())
    return JSONResponse(status_code=422, content={'run_id': run['id'], 'detail': errors})

def now():
    return datetime.now(timezone.utc).isoformat()


def progress(run_id, processed, total, message, started, status='running', output=None):
    payload = dict(processed=processed, total=total, message=message, status=status)
    result = output if output is not None else payload
    with connect() as db:
        db.execute('INSERT INTO events(run_id,timestamp,payload) VALUES(?,?,?)', (run_id, now(), json.dumps(payload)))
        db.execute('UPDATE runs SET status=?, output=?, duration_ms=? WHERE id=?', (status, json.dumps(result), round((time.perf_counter()-started)*1000,3), run_id))
    log.info(json.dumps(dict(event='run_progress', run_id=run_id, **payload)))
    if status != 'running':
        log.info(json.dumps(dict(event='analysis_result', run_id=run_id, status=status, output=result)))


def work(run_id, args):
    started = time.perf_counter()
    words = re.findall(r"\b\w+(?:['’-]\w+)*\b", args.text.lower())
    processed = 0
    try:
        for processed in range(1,len(words)+1):
            if app.state.stop.wait(1):
                progress(run_id, processed-1, len(words), 'Service stopped before completion', started, 'interrupted', {'error':'Service stopped before completion'})
                return
            progress(run_id, processed, len(words), f'Processed {processed} of {len(words)} words', started)
        progress(run_id, len(words), len(words), 'Analysis complete', started, 'success', analyze(args))
    except Exception:
        log.exception('Background analysis failed')
        try:
            progress(run_id, processed, len(words), 'Analysis failed', started, 'error', {'error':'Analysis failed'})
        except Exception:
            log.exception('Could not persist failure; active lock retained until restart')


@app.post('/analyze', status_code=202)
def analyze_text(args: AnalysisInput):
    run_id = str(uuid.uuid4())
    total = len(re.findall(r"\b\w+(?:['’-]\w+)*\b", args.text))
    payload = dict(processed=0, total=total, message=f'Starting analysis: {total} words', status='running')
    try:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute("SELECT id FROM runs WHERE status='running'").fetchone()
            if active:
                return JSONResponse(status_code=409, content={'detail':'Another analysis is already running', 'run_id':active['id']})
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)', (run_id, now(), json.dumps(args.model_dump()), json.dumps(payload), 0, 'running'))
            db.execute('INSERT INTO events(run_id,timestamp,payload) VALUES(?,?,?)', (run_id, now(), json.dumps(payload)))
    except sqlite3.Error:
        log.exception('Could not create audited job')
        raise HTTPException(503, 'Audit database unavailable')
    log.info(json.dumps(dict(event='analysis_started', run_id=run_id, inputs=args.model_dump())))
    try:
        app.state.worker.submit(work, run_id, args)
    except Exception:
        progress(run_id, 0, total, 'Could not start worker', time.perf_counter(), 'error', {'error':'Could not start worker'})
        raise HTTPException(503, 'Could not start worker')
    return {'run_id':run_id, 'status':'running'}


@app.get('/active-run')
def active_run():
    with connect() as db:
        row = db.execute("SELECT id FROM runs WHERE status='running'").fetchone()
    return {'run_id':row['id'] if row else None}


@app.get('/runs/{run_id}/events')
async def run_events(run_id: uuid.UUID, request: Request, after: int = Query(0, ge=0)):
    get_run(run_id)
    try:
        cursor = max(after, int(request.headers.get('last-event-id','0')))
    except ValueError:
        raise HTTPException(422, 'Invalid event cursor')
    def read_events(cursor):
        with connect() as db:
            db.execute('BEGIN')
            rows = db.execute('SELECT * FROM events WHERE run_id=? AND sequence>? ORDER BY sequence', (str(run_id),cursor)).fetchall()
            status = db.execute('SELECT status FROM runs WHERE id=?',(str(run_id),)).fetchone()[0]
        return rows, status
    async def stream():
        nonlocal cursor
        ticks = 0
        while not await request.is_disconnected():
            rows, status = await asyncio.to_thread(read_events, cursor)
            for row in rows:
                cursor = row['sequence']
                payload = json.loads(row['payload'])
                payload.update(sequence=cursor, timestamp=row['timestamp'])
                yield 'id: '+str(cursor)+'\ndata: '+json.dumps(payload)+'\n\n'
            if status != 'running':
                yield 'event: done\ndata: '+json.dumps({'status':status})+'\n\n'
                return
            if ticks % 40 == 0:
                yield ': keepalive\n\n'
            ticks += 1
            await asyncio.sleep(.25)
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control':'no-cache', 'X-Accel-Buffering':'no'})


def decode(row):
    result = dict(row)
    result['inputs'] = json.loads(result['inputs'])
    result['output'] = json.loads(result['output'])
    return result

@app.get('/health')
def health():
    try:
        with connect() as db:
            db.execute('SELECT 1 FROM runs LIMIT 1')
    except sqlite3.Error:
        raise HTTPException(503, 'Audit database unavailable')
    return {'status': 'ok'}

@app.get('/runs')
def runs(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), q: str = Query('', max_length=200), status: str = Query('', pattern='^(running|success|error|validation_error|interrupted)?$'), from_date: str = Query('', alias='from'), to_date: str = Query('', alias='to')):
    from datetime import date, timedelta
    clauses, values = [], []
    try:
        start = date.fromisoformat(from_date) if from_date else None
        end = date.fromisoformat(to_date) if to_date else None
        if start and end and start > end:
            raise ValueError('Date range reversed')
        if start:
            clauses.append('timestamp >= ?'); values.append(start.isoformat())
        if end:
            clauses.append('timestamp < ?'); values.append((end + timedelta(days=1)).isoformat())
    except (ValueError, OverflowError):
        raise HTTPException(422, 'Use a valid date range in YYYY-MM-DD format')
    if q:
        clauses.append('(instr(lower(inputs), lower(?)) > 0 OR instr(lower(output), lower(?)) > 0)'); values.extend([q, q])
    if status:
        clauses.append('status = ?'); values.append(status)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    with connect() as db:
        total = db.execute('SELECT count(*) FROM runs' + where, values).fetchone()[0]
        rows = db.execute('SELECT * FROM runs' + where + ' ORDER BY rowid DESC LIMIT ? OFFSET ?', [*values, limit, offset]).fetchall()
    return {'runs': [decode(row) for row in rows], 'total': total, 'limit': limit, 'offset': offset}


@app.get('/runs/{run_id}')
def get_run(run_id: uuid.UUID):
    with connect() as db:
        row = db.execute('SELECT * FROM runs WHERE id = ?', (str(run_id),)).fetchone()
    if row is None:
        raise HTTPException(404, 'Run not found')
    return decode(row)


@app.get("/", include_in_schema=False)
def root():
    return HTMLResponse(Path("/app/index.html").read_text())


@app.get('/history', include_in_schema=False)
@app.get('/history/{run_id}', include_in_schema=False)
def history_page():
    return HTMLResponse(Path('/app/history.html').read_text())
