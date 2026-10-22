// 앱 껍데기는 캐시해 두고(오프라인에서도 열림), data.json 은 항상 네트워크 우선으로 받는다.
const CACHE = 'watchlist-shell-v1';
const SHELL = ['./', 'index.html', 'manifest.webmanifest', 'icon-192.png', 'icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET' || new URL(req.url).origin !== location.origin) return;
  if (req.url.includes('data.json')) return;                  // 데이터는 캐시하지 않음 (앱이 자체 저장본으로 대체)
  e.respondWith(
    fetch(req).then(res => {                                   // 네트워크 우선: 앱을 고치면 바로 반영
      const copy = res.clone();
      caches.open(CACHE).then(c => c.put(req, copy));
      return res;
    }).catch(() => caches.match(req).then(r => r || caches.match('index.html')))
  );
});
