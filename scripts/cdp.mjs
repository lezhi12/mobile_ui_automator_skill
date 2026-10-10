// Minimal CDP Runtime.evaluate client for the local WebView forwarding.
// Usage: node cdp.mjs <exprFile> [targetId] [port]   (port: tcp:0 临时端口，默认 9222)
import http from 'http';
import fs from 'fs';

function getJson(url) {
  return new Promise((resolve, reject) => {
    http.get(url, res => { let d = ''; res.on('data', c => d += c); res.on('end', () => resolve(JSON.parse(d))); })
       .on('error', reject);
  });
}

(async () => {
  const expr = fs.readFileSync(process.argv[2], 'utf-8');
  const targetId = process.argv[3];
  const port = process.argv[4] || '9222';
  const targets = await getJson(`http://127.0.0.1:${port}/json`);
  const page = targetId ? targets.find(t => t.id === targetId)
                        : targets.find(t => t.type === 'page');
  if (!page) { console.error('no page target'); process.exit(1); }

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.addEventListener('open', res, {once: true}); ws.addEventListener('error', rej, {once: true}); });
  ws.send(JSON.stringify({id: 1, method: 'Runtime.evaluate',
    params: {expression: expr, returnByValue: true, awaitPromise: true}}));

  await new Promise((resolve, reject) => {
    ws.addEventListener('message', ev => {
      const m = JSON.parse(ev.data);
      if (m.id === 1) {
        console.log(JSON.stringify(m.result, null, 2));
        ws.close(); resolve();
      }
    });
    ws.addEventListener('error', reject);
  });
  process.exit(0);
})();
