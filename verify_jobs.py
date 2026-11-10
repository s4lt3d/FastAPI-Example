import concurrent.futures, json, os, time, urllib.request, urllib.error
from pathlib import Path
BASE=os.getenv('API_BASE_URL', 'http://192.168.1.31:8000').rstrip('/')
def req(path, data=None, headers=None):
    request=urllib.request.Request(BASE+path, data=json.dumps(data).encode() if data is not None else None, headers={'Content-Type':'application/json',**(headers or {})})
    try:
        with urllib.request.urlopen(request,timeout=15) as r:return r.status,json.load(r)
    except urllib.error.HTTPError as e:return e.code,json.load(e)
def submit(_):return req('/analyze',{'text':'one two three four', 'include_word_frequency':True})
started=time.monotonic()
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(submit,range(6)))
assert sum(code==202 for code,data in results)==1, results
assert sum(code==409 for code,data in results)==5, results
run_id=next(data['run_id'] for code,data in results if code==202)
assert all(data['run_id']==run_id for code,data in results)
assert req('/active-run')[1]['run_id']==run_id
assert req('/health')[0]==200
assert req('/runs/'+run_id)[1]['status']=='running'
with urllib.request.urlopen(BASE+'/runs/'+run_id+'/events',timeout=15) as stream:
    frames=stream.read().decode()
events=[json.loads(line[6:]) for line in frames.splitlines() if line.startswith('data: ') and 'sequence' in line]
assert [e['processed'] for e in events]==[0,1,2,3,4,4],events
assert 'event: done' in frames
assert time.monotonic()-started>=4
run=req('/runs/'+run_id)[1]
assert run['status']=='success' and run['output']['word_count']==4 and run['output']['word_frequency']['one']==1
assert run['duration_ms']>=4000
cursor=events[2]['sequence']
request=urllib.request.Request(BASE+'/runs/'+run_id+'/events',headers={'Last-Event-ID':str(cursor)})
with urllib.request.urlopen(request,timeout=15) as stream:
    replay=stream.read().decode()
replayed=[json.loads(line[6:]) for line in replay.splitlines() if line.startswith('data: ') and 'sequence' in line]
assert all(e['sequence']>cursor for e in replayed) and len(replayed)==3
assert req('/active-run')[1]['run_id'] is None
code,error=req('/analyze',{'text':'','words_per_minute':0})
assert code==422 and req('/runs/'+error['run_id'])[1]['status']=='validation_error'
assert req('/runs?status=running')[1]['total']==0
assert req('/runs?status=success&q=one')[1]['total']>=1
print('PASS: concurrent submissions, responsiveness, one-second progress, final output, SSE replay/resume, saved audit, lock release, validation')
Path('verified-run-id.txt').write_text(run_id)
