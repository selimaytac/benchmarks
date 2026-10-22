"""One arm, one workload, one process. Prints one JSON line.

Usage: python bench.py <arm> <mode> <calls> <workers> <prompt_bytes>
  arm: baseline | openllmetry | openinference | langfuse   (run with that arm's venv)
  mode: plain | stream
Each arm uses its SDK's defaults (what you get out of the box), exporting to the local receiver.
"""
import json, os, resource, sys, threading, time, urllib.request

arm, mode, calls, workers, prompt_bytes = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
API, RECV = 'http://127.0.0.1:8090/v1', 'http://127.0.0.1:4318'
os.environ.update(OPENAI_API_KEY='sk-mock', OPENAI_BASE_URL=API)

t0 = time.perf_counter()
flush = lambda: None
if arm == 'baseline':
    from openai import OpenAI
elif arm == 'openllmetry':
    os.environ['TRACELOOP_TELEMETRY'] = 'false'
    from traceloop.sdk import Traceloop
    from openai import OpenAI
    Traceloop.init(app_name='bench', api_endpoint=RECV, telemetry_enabled=False)
    from opentelemetry import trace
    flush = lambda: trace.get_tracer_provider().force_flush()
elif arm == 'openinference':
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from openinference.instrumentation.openai import OpenAIInstrumentor
    from openai import OpenAI
    tp = TracerProvider()
    tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=RECV + '/v1/traces')))
    OpenAIInstrumentor().instrument(tracer_provider=tp)
    flush = tp.force_flush
elif arm == 'langfuse':
    os.environ.update(LANGFUSE_BASE_URL=RECV, LANGFUSE_PUBLIC_KEY='pk-lf-bench', LANGFUSE_SECRET_KEY='sk-lf-bench')
    from langfuse.openai import OpenAI
    from langfuse import get_client
    flush = lambda: get_client().flush()
init_s = time.perf_counter() - t0

client = OpenAI()
import random  # non-repeating words, so gzip ratios look like real prompts, not like 'lorem ipsum' x 1000
rng = random.Random(7)
vocab = [''.join(rng.choice('etaoinshrdlucmfwypvbgkjqxz') for _ in range(rng.randint(2, 9))) for _ in range(3000)]
# a different prompt for every call, built before timing (real apps don't send the same prompt 5,000 times)
prompts = [' '.join(rng.choice(vocab) for _ in range(prompt_bytes // 4))[:prompt_bytes] for _ in range(calls + 300)]
nxt = iter(range(len(prompts)))
lock = threading.Lock()

def one():
    with lock:
        i = next(nxt)
    messages = [{'role': 'system', 'content': 'You are terse.'}, {'role': 'user', 'content': prompts[i]}]
    s = time.perf_counter_ns()
    if mode == 'stream':
        for _ in client.chat.completions.create(model='gpt-mock', messages=messages, stream=True,
                                                stream_options={'include_usage': True}):
            pass
    else:
        client.chat.completions.create(model='gpt-mock', messages=messages)
    return time.perf_counter_ns() - s

if os.environ.get('BENCH_FIRST_CALL_ONLY'):  # startup.py: exit right after the first completed call
    one(); flush(); sys.exit(0)
for _ in range(300):  # warm-up
    one()
flush(); time.sleep(0.5)
urllib.request.urlopen(RECV + '/reset').read()

lat = []
def worker(n):
    out = [one() for _ in range(n)]
    lat.extend(out)
cpu0 = time.process_time(); w0 = time.perf_counter()
ts = [threading.Thread(target=worker, args=(calls // workers,)) for _ in range(workers)]
[t.start() for t in ts]; [t.join() for t in ts]
wall = time.perf_counter() - w0; cpu = time.process_time() - cpu0
flush(); time.sleep(1.0)
stats = json.loads(urllib.request.urlopen(RECV + '/stats').read())
lat.sort()
q = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))] / 1000  # microseconds
print(json.dumps({'arm': arm, 'mode': mode, 'calls': len(lat), 'workers': workers, 'prompt_bytes': prompt_bytes,
                  'p50_us': q(0.5), 'p90_us': q(0.9), 'p99_us': q(0.99), 'mean_us': sum(lat) / len(lat) / 1000,
                  'calls_per_s': len(lat) / wall, 'cpu_us_per_call': cpu / len(lat) * 1e6,
                  'peak_rss_mb': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6, 'init_s': init_s,
                  'export': stats, 'export_bytes_per_call': sum(v['bytes'] for v in stats.values()) / len(lat),
                  'export_raw_bytes_per_call': sum(v['raw_bytes'] for v in stats.values()) / len(lat)}))
