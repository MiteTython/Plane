// Vault shell cache · network-first for the page, cache-first for icons & manifest
const V='vault-shell-v2';const SHELL=['./','./index.html','./manifest.webmanifest','./icons/chase.png','./icons/ally.png','./icons/sofi.png','./icons/chase-dark.png','./icons/ally-dark.png','./icons/chase-mark.png','./icons/ally-mark.png','./icons/sofi-mark.png','./icons/vault-v09.png','./icons/vault-v09light.png'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(V).then(c=>c.addAll(SHELL).catch(()=>{})));self.skipWaiting();});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==V).map(k=>caches.delete(k)))));self.clients.claim();});
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(e.request.method!=='GET'||u.origin!==location.origin)return;
  if(u.pathname.endsWith('/')||u.pathname.endsWith('index.html')||u.pathname.endsWith('sw.js')){e.respondWith(fetch(e.request).then(r=>{const cp=r.clone();caches.open(V).then(c=>c.put(e.request,cp));return r;}).catch(()=>caches.match(e.request).then(r=>r||caches.match('./index.html'))));return;}
  e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request).then(res=>{const cp=res.clone();caches.open(V).then(c=>c.put(e.request,cp));return res;})));});
