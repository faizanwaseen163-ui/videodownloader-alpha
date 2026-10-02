/**
 * VideoDownloader — Node.js web server + reverse proxy.
 * Uses only Node built-ins (no node_modules needed).
 *   - Serves index.html at "/" and "/index.html"
 *   - Proxies every /api/* request to Flask at 127.0.0.1:FLASK_PORT (no buffering)
 *   - Passes SSE streams through unbuffered
 *   - Injects X-Real-IP from the real socket so Flask can detect the host PC
 *   - Exposes /node-health for the LAN self-test
 */

'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');
const os = require('os');
const url = require('url');

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
const NODE_PORT = parseInt(process.env.VD_NODE_PORT || '7421', 10);
const FLASK_PORT = parseInt(process.env.VD_FLASK_PORT || '7422', 10);
const FLASK_HOST = '127.0.0.1';
const PROJECT_DIR = __dirname;
const INDEX_FILE = path.join(PROJECT_DIR, 'index.html');

const START_TIME = Date.now();

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------
function log(...args) {
  const ts = new Date().toISOString();
  console.log(`[${ts}] [node]`, ...args);
}

function normalizeRemoteIp(addr) {
  if (!addr) return '';
  if (addr.startsWith('::ffff:')) return addr.slice(7);
  return addr;
}

function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-cache',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

// ---------------------------------------------------------------------------
// Static index.html
// ---------------------------------------------------------------------------
function serveIndex(req, res) {
  fs.readFile(INDEX_FILE, (err, data) => {
    if (err) {
      log('index.html read failed:', err.message);
      sendJson(res, 500, {
        ok: false,
        error: { code: 'INDEX_MISSING', message: 'index.html not found next to server.js' },
      });
      return;
    }
    res.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'no-cache, no-store, must-revalidate',
      'Content-Length': data.length,
    });
    res.end(data);
  });
}

// ---------------------------------------------------------------------------
// Reverse proxy — /api/*  →  Flask
// ---------------------------------------------------------------------------
function proxyToFlask(req, res) {
  const parsed = url.parse(req.url);

  // Inject real client IP (strip any spoofed forwarding headers).
  const realIp = normalizeRemoteIp(req.socket.remoteAddress || '');

  const headers = Object.assign({}, req.headers);
  delete headers['x-real-ip'];
  delete headers['x-forwarded-for'];
  delete headers['x-forwarded-host'];
  delete headers['x-forwarded-proto'];
  headers['x-real-ip'] = realIp;
  headers['host'] = `${FLASK_HOST}:${FLASK_PORT}`;

  const options = {
    hostname: FLASK_HOST,
    port: FLASK_PORT,
    path: parsed.path,
    method: req.method,
    headers,
  };

  const upstream = http.request(options, (upRes) => {
    // Is this an SSE stream?
    const ct = String(upRes.headers['content-type'] || '');
    const isSSE = ct.includes('text/event-stream');

    const outHeaders = Object.assign({}, upRes.headers);
    if (isSSE) {
      outHeaders['content-type'] = 'text/event-stream; charset=utf-8';
      outHeaders['cache-control'] = 'no-cache, no-transform';
      outHeaders['connection'] = 'keep-alive';
      outHeaders['x-accel-buffering'] = 'no';
      delete outHeaders['content-length'];
      delete outHeaders['content-encoding'];
    }

    res.writeHead(upRes.statusCode || 502, outHeaders);

    if (isSSE) {
      // Flush headers immediately and disable buffering.
      if (typeof res.flushHeaders === 'function') res.flushHeaders();
      try {
        req.socket.setNoDelay(true);
        req.socket.setKeepAlive(true);
      } catch (_) {}
    }

    upRes.pipe(res);

    // If the browser closes, tear down the upstream.
    req.on('close', () => {
      try { upRes.destroy(); } catch (_) {}
    });
    upRes.on('error', (e) => {
      log('upstream response error:', e.message);
      try { res.end(); } catch (_) {}
    });
    upRes.on('end', () => {
      try { res.end(); } catch (_) {}
    });
  });

  upstream.on('error', (e) => {
    log('proxy error to Flask:', e.message);
    if (!res.headersSent) {
      sendJson(res, 502, {
        ok: false,
        error: { code: 'BACKEND_DOWN', message: 'Backend is not reachable. It will restart automatically.' },
      });
    } else {
      try { res.end(); } catch (_) {}
    }
  });

  // Stream the request body with no buffering.
  req.pipe(upstream);
}

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------
const server = http.createServer((req, res) => {
  const parsed = url.parse(req.url);
  const pathname = parsed.pathname || '/';

  // Allow only the methods we actually use.
  const method = req.method || 'GET';
  const allowed = ['GET', 'POST', 'DELETE', 'OPTIONS', 'HEAD'];
  if (!allowed.includes(method)) {
    sendJson(res, 405, { ok: false, error: { code: 'METHOD_NOT_ALLOWED', message: 'Method not allowed' } });
    return;
  }

  // CORS pre-flight (harmless, helpful for browsers).
  if (method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      'Access-Control-Max-Age': '600',
    });
    res.end();
    return;
  }

  // Node-only health check for the LAN self-test.
  if (pathname === '/node-health') {
    sendJson(res, 200, {
      ok: true,
      service: 'node',
      uptime_s: Math.floor((Date.now() - START_TIME) / 1000),
    });
    return;
  }

  // Static index.
  if (pathname === '/' || pathname === '/index.html') {
    serveIndex(req, res);
    return;
  }

  // API proxy.
  if (pathname.startsWith('/api/')) {
    proxyToFlask(req, res);
    return;
  }

  // Everything else → JSON 404.
  sendJson(res, 404, {
    ok: false,
    error: { code: 'NOT_FOUND', message: `Path not found: ${pathname}` },
  });
});

// ---------------------------------------------------------------------------
// Server timeouts for long-lived SSE
// ---------------------------------------------------------------------------
server.requestTimeout = 0;
server.headersTimeout = 65000;
server.keepAliveTimeout = 65000;

server.on('connection', (socket) => {
  try {
    socket.setNoDelay(true);
    socket.setKeepAlive(true, 30000);
    socket.setTimeout(0);
  } catch (_) {}
});

// ---------------------------------------------------------------------------
// Robustness: never crash
// ---------------------------------------------------------------------------
process.on('uncaughtException', (err) => {
  log('uncaughtException:', err && err.stack ? err.stack : err);
});
process.on('unhandledRejection', (reason) => {
  log('unhandledRejection:', reason);
});

// ---------------------------------------------------------------------------
// Graceful shutdown
// ---------------------------------------------------------------------------
function shutdown(sig) {
  log(`received ${sig}, shutting down`);
  try { server.close(() => process.exit(0)); } catch (_) { process.exit(0); }
  setTimeout(() => process.exit(0), 2000).unref();
}
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
if (process.platform === 'win32') {
  // Windows Ctrl+Break / Ctrl+C mapping for detached processes.
  try { process.on('SIGBREAK', () => shutdown('SIGBREAK')); } catch (_) {}
}

// ---------------------------------------------------------------------------
// Listen on 0.0.0.0 (network-visible, works on every adapter)
// ---------------------------------------------------------------------------
server.listen(NODE_PORT, '0.0.0.0', () => {
  log(`listening on 0.0.0.0:${NODE_PORT} → proxying /api/* to http://${FLASK_HOST}:${FLASK_PORT}`);
  log(`open http://localhost:${NODE_PORT} on this PC`);
  // Best-effort: print LAN URLs (Flask will refine them in /api/network).
  try {
    const ifaces = os.networkInterfaces();
    for (const name of Object.keys(ifaces)) {
      for (const info of ifaces[name] || []) {
        if (info.family === 'IPv4' && !info.internal) {
          log(`LAN: http://${info.address}:${NODE_PORT}  (${name})`);
        }
      }
    }
  } catch (_) {}
});