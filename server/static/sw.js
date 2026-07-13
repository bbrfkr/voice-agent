/* Service Worker: アプリシェルをキャッシュして起動を速くし、インストール可能にする。
 *
 * 方針:
 *  - キャッシュ対象は静的なシェル（HTML/CSS/JS/アイコン/マニフェスト）のみ。
 *  - /api/ と /ws（WebSocket）は動的なので絶対に横取りしない（常にネットワーク直行）。
 *  - シェルは stale-while-revalidate: まずキャッシュを返しつつ裏で更新を取りに行くので、
 *    開発中に app.js 等を更新しても次回リロードで反映される。
 *  - ナビゲーション（HTML）は network-first にし、オフライン時のみキャッシュした index を返す。
 *  - CACHE_VERSION を上げると古いキャッシュを一掃する。
 */
const CACHE_VERSION = "v1";
const CACHE_NAME = `voice-agent-shell-${CACHE_VERSION}`;

// プリキャッシュするシェル資産（相対パスはスコープ基準）。
const SHELL_ASSETS = [
  "/",
  "/index.html",
  "/styles.css",
  "/app.js",
  "/marked.min.js",
  "/purify.min.js",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-maskable-512.png",
  "/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  // 新しい SW をすぐ待機解除して次の制御へ回す。
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)).then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  // 旧バージョンのキャッシュを削除し、開いている全タブを即座に制御下に置く。
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;

  // GET 以外・別オリジンは素通し（POST な API や外部リソースを壊さない）。
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // 動的エンドポイント（API / WebSocket アップグレード）は横取りしない。
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws")) return;

  // ナビゲーション（ページ遷移）は network-first、失敗時にキャッシュへフォールバック。
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() => caches.match("/index.html").then((r) => r || caches.match("/"))),
    );
    return;
  }

  // それ以外の同一オリジン GET（シェル資産）は stale-while-revalidate。
  event.respondWith(
    caches.open(CACHE_NAME).then((cache) =>
      cache.match(req).then((cached) => {
        const network = fetch(req)
          .then((res) => {
            if (res && res.status === 200 && res.type === "basic") {
              cache.put(req, res.clone());
            }
            return res;
          })
          .catch(() => cached);
        return cached || network;
      }),
    ),
  );
});
