"""Graphicle 0.1.5 rerun of issue #8: sync time, `graphicle context` time, the real prompt hook within its budget,
and whether the question's file name appears in the packet. Same kubernetes clone and 22 questions as the 0.1.3 run.
Usage: REPO=<clone> GRAPHICLE_DATA_DIR=<empty dir> [ROUNDS=3] python3 run.py
The prompt hook is run the way setup --prompt-context installs it: `graphicle hook claude-code prompt-context`.
"""
import json, os, re, subprocess, time
from pathlib import Path

HERE = Path(__file__).parent
REPO = os.environ['REPO']
ENV = dict(os.environ, GRAPHICLE_NO_UPDATE_NOTIFIER='1')
ROUNDS = int(os.environ.get('ROUNDS', '3'))
QUESTIONS = json.loads((HERE / 'questions.json').read_text())['questions']
out = HERE / "results-0.1.5.json"
res = json.loads(out.read_text()) if out.exists() else {}

def timed(cmd, **kw):
    t = time.time(); p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, env=ENV, **kw); return p, round(time.time() - t, 2)

if 'sync' not in res:
    p, s = timed(['graphicle', 'sync', REPO, '--json'])
    res['sync'] = {'seconds': s, 'rc': p.returncode}
    out.write_text(json.dumps(res, indent=1)); print('sync', s, flush=True)

for q in QUESTIONS:
    if q['id'] in res: continue
    secs = []
    for _ in range(ROUNDS):
        p, s = timed(['graphicle', 'context', q['q']]); secs.append(s)
    md = json.loads(p.stdout)['markdown'] if p.returncode == 0 and p.stdout.strip() else ''
    hook_in = json.dumps({'session_id': 'bench', 'transcript_path': '/dev/null', 'cwd': REPO,
                          'hook_event_name': 'UserPromptSubmit', 'prompt': q['q']})
    h, hs = timed(['graphicle', 'hook', 'claude-code', 'prompt-context'], input=hook_in)
    hook_ctx = ''
    try: hook_ctx = json.loads(h.stdout)['hookSpecificOutput']['additionalContext'] if h.stdout.strip() else ''
    except Exception: hook_ctx = h.stdout
    first = q['key'][0]  # the file group of the key: the first file the fix changed
    res[q['id']] = {'context_s': sorted(secs)[len(secs) // 2], 'context_rounds': secs, 'context_names_file': any(re.search(k, md) for k in first),
                    'hook_s': hs, 'hook_packet': bool(hook_ctx.strip()), 'hook_names_file': any(re.search(k, hook_ctx) for k in first),
                    'packet_chars': len(md)}
    out.write_text(json.dumps(res, indent=1)); print(q['id'], res[q['id']], flush=True)
