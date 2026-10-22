// Mock OpenAI chat API (port 8090) + OTLP receiver that counts and drops (port 4318).
// Both answer instantly so the only difference between arms is the tracing work in the client.
import http from 'node:http';
import zlib from 'node:zlib';
import fs from 'node:fs';
const DUMP = process.env.DUMP_DIR;  // if set, save the first decoded body per path for inspection

const reply = (content) => ({
  id: 'chatcmpl-mock', object: 'chat.completion', created: 1, model: 'gpt-mock',
  choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }],
  usage: { prompt_tokens: 50, completion_tokens: 50, total_tokens: 100 },
});
const TEXT = 'ok '.repeat(50).trim();

http.createServer((req, res) => {
  let body = '';
  req.on('data', (c) => (body += c));
  req.on('end', () => {
    const stream = body.includes('"stream":true') || body.includes('"stream": true');
    if (!stream) {
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify(reply(TEXT)));
    }
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    for (let i = 0; i < 50; i++) {
      const chunk = { id: 'chatcmpl-mock', object: 'chat.completion.chunk', created: 1, model: 'gpt-mock',
        choices: [{ index: 0, delta: i === 0 ? { role: 'assistant', content: 'ok ' } : { content: 'ok ' }, finish_reason: null }] };
      res.write(`data: ${JSON.stringify(chunk)}\n\n`);
    }
    const last = { id: 'chatcmpl-mock', object: 'chat.completion.chunk', created: 1, model: 'gpt-mock',
      choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage: { prompt_tokens: 50, completion_tokens: 50, total_tokens: 100 } };
    res.write(`data: ${JSON.stringify(last)}\n\ndata: [DONE]\n\n`);
    res.end();
  });
}).listen(8090);

let stats = {};
http.createServer((req, res) => {
  if (req.url === '/stats') {
    res.writeHead(200, { 'content-type': 'application/json' });
    return res.end(JSON.stringify(stats));
  }
  if (req.url === '/reset') { stats = {}; res.writeHead(200); return res.end('{}'); }
  const parts = [];
  req.on('data', (c) => parts.push(c));
  req.on('end', () => {
    const buf = Buffer.concat(parts);
    const enc = req.headers['content-encoding'] || '';
    let raw = buf.length;
    try { if (enc.includes('gzip')) raw = zlib.gunzipSync(buf).length; else if (enc.includes('deflate')) raw = zlib.inflateSync(buf).length; } catch {}
    const k = req.method + ' ' + req.url.split('?')[0] + (enc ? ' [' + enc + ']' : '');
    if (DUMP && !stats[k]) {
      let body = buf;
      try { if (enc.includes('gzip')) body = zlib.gunzipSync(buf); } catch {}
      fs.writeFileSync(`${DUMP}/${req.url.split('?')[0].replace(/\//g, '_')}.bin`, body);
    }
    stats[k] = stats[k] || { requests: 0, bytes: 0, raw_bytes: 0 };
    stats[k].requests++; stats[k].bytes += buf.length; stats[k].raw_bytes += raw;
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('{}');
  });
}).listen(4318);
console.log('mock ready');
