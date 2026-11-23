#!/usr/bin/env node
/**
 * Matthew's Badge Vault — web server
 * Serves the collection website. Badge data is read from GitHub on each request
 * (60s cache) so the site updates automatically when new badges are pushed.
 */

const http  = require('http');
const https = require('https');
const fs    = require('fs');
const path  = require('path');

const PORT           = process.env.PORT || 3000;
const GITHUB_RAW     = 'https://raw.githubusercontent.com/airmax617/matthew-badges/main';
const COLLECTION_URL = `${GITHUB_RAW}/state/badge-collection.json`;
const CACHE_TTL      = 60000;
// Pages are read from GitHub (so a push goes live within a minute), with the deployed copy as a fallback.
// HTML_SOURCE=local reads the files on disk instead (local testing).
const PAGES = { '/': 'public/home.html', '/vault': 'public/index.html' };
const MEDIA_DIR = path.join(__dirname, 'public', 'media');
const TYPES = { '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp', '.mp4': 'video/mp4', '.svg': 'image/svg+xml' };

let _dataCache = null, _dataCacheAt = 0;
const _htmlCache = {};

function fetchGithub(url) {
  return new Promise((resolve, reject) => {
    https.get(url, { timeout: 8000 }, res => {
      let body = '';
      res.on('data', c => body += c);
      res.on('end', () => resolve(body));
    }).on('error', reject).on('timeout', () => reject(new Error('GitHub timeout')));
  });
}

async function fetchCollection() {
  if (_dataCache && Date.now() - _dataCacheAt < CACHE_TTL) return _dataCache;
  const body = await fetchGithub(COLLECTION_URL);
  _dataCache = JSON.parse(body);
  _dataCacheAt = Date.now();
  return _dataCache;
}

async function fetchHtml(file) {
  const c = _htmlCache[file];
  if (c && Date.now() - c.at < CACHE_TTL) return c.body;
  let body;
  if (process.env.HTML_SOURCE !== 'local') {
    try {
      body = await fetchGithub(`${GITHUB_RAW}/${file}`);
      if (!/<html/i.test(body)) body = null;   // 404 text or an error page
    } catch (e) { body = null; }
  }
  if (!body) body = fs.readFileSync(path.join(__dirname, file), 'utf8');
  _htmlCache[file] = { body, at: Date.now() };
  return body;
}

// Static photos/videos from public/media, with Range support (Safari needs it to play mp4).
function serveMedia(req, res, urlPath) {
  let rel;
  try { rel = decodeURIComponent(urlPath.slice('/media/'.length)); } catch (e) { return send(res, 400, 'text/plain', 'bad path'); }
  const file = path.join(MEDIA_DIR, rel);
  if (!file.startsWith(MEDIA_DIR + path.sep)) return send(res, 404, 'text/plain', 'not found');
  fs.stat(file, (err, st) => {
    if (err || !st.isFile()) return send(res, 404, 'text/plain', 'not found');
    const type = TYPES[path.extname(file).toLowerCase()] || 'application/octet-stream';
    const base = { 'Content-Type': type, 'Accept-Ranges': 'bytes', 'Cache-Control': 'public, max-age=86400' };
    const m = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range || '');
    if (m && (m[1] || m[2])) {
      let start = m[1] ? +m[1] : Math.max(0, st.size - +m[2]);
      let end = m[1] && m[2] ? Math.min(+m[2], st.size - 1) : st.size - 1;
      if (start >= st.size || start > end) { res.writeHead(416, { 'Content-Range': `bytes */${st.size}` }); return res.end(); }
      res.writeHead(206, { ...base, 'Content-Range': `bytes ${start}-${end}/${st.size}`, 'Content-Length': end - start + 1 });
      if (req.method === 'HEAD') return res.end();
      return fs.createReadStream(file, { start, end }).pipe(res);
    }
    res.writeHead(200, { ...base, 'Content-Length': st.size });
    if (req.method === 'HEAD') return res.end();
    fs.createReadStream(file).pipe(res);
  });
}

function send(res, status, contentType, body) {
  res.writeHead(status, {
    'Content-Type': contentType,
    'Access-Control-Allow-Origin': '*',
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

http.createServer(async (req, res) => {
  const urlPath = req.url.split('?')[0];
  if ((req.method === 'GET' || req.method === 'HEAD') && urlPath.startsWith('/media/')) return serveMedia(req, res, urlPath);

  const page = PAGES[urlPath.replace(/\/+$/, '') || '/'];
  if (req.method === 'GET' && page) {
    try {
      const html = await fetchHtml(page);
      send(res, 200, 'text/html; charset=utf-8', html);
    } catch(e) {
      send(res, 502, 'text/plain', 'Could not load page. Try refreshing.');
    }
    return;
  }

  if (req.method === 'GET' && urlPath === '/api/badges') {
    try {
      const data = await fetchCollection();
      send(res, 200, 'application/json', JSON.stringify(data));
    } catch(e) {
      send(res, 502, 'application/json', JSON.stringify({ error: e.message }));
    }
    return;
  }

  send(res, 404, 'application/json', JSON.stringify({ error: 'not found' }));

}).listen(PORT, () => {
  console.log(`Matthew's Badge Vault → http://localhost:${PORT}`);
});
