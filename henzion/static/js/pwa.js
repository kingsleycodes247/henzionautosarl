/* PWA glue: service worker, "install app" button and phone notifications. */
(function () {
  const body = document.body;
  const csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  const PUSH_AVAILABLE = body.dataset.push === '1';
  const strip = document.getElementById('pwa-strip');
  const stripText = document.getElementById('pwa-strip-text');
  const installBtns = document.querySelectorAll('[data-install]');
  const pushBtns = document.querySelectorAll('[data-push-toggle]');
  const iosHint = document.getElementById('pwa-ios-hint');

  const isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const store = {
    get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };

  function post(url, data) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
      body: JSON.stringify(data || {}),
    }).then((r) => r.json().catch(() => ({ ok: false })));
  }
  function b64ToBytes(s) {
    const pad = '='.repeat((4 - (s.length % 4)) % 4);
    const raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }

  // ------------------------------------------------------------ service worker
  let registration = null;
  const swReady = ('serviceWorker' in navigator)
    ? navigator.serviceWorker.register('/sw.js', { scope: '/' }).then((r) => (registration = r)).catch(() => null)
    : Promise.resolve(null);

  // ------------------------------------------------------------ strip (one message at a time)
  function showStrip(message, key) {
    if (!strip || store.get('pwa-dismissed-' + key)) return;
    stripText.textContent = message;
    strip.dataset.key = key;
    strip.classList.remove('hidden');
  }
  if (strip) {
    strip.querySelector('[data-dismiss]').addEventListener('click', () => {
      store.set('pwa-dismissed-' + strip.dataset.key, '1');
      strip.classList.add('hidden');
    });
  }

  // ------------------------------------------------------------ install
  let deferred = null;
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferred = e;
    installBtns.forEach((b) => b.classList.remove('hidden'));
  });
  window.addEventListener('appinstalled', () => {
    deferred = null;
    installBtns.forEach((b) => b.classList.add('hidden'));
  });
  installBtns.forEach((b) => b.addEventListener('click', async () => {
    if (!deferred) return;
    deferred.prompt();
    await deferred.userChoice;
    deferred = null;
    installBtns.forEach((x) => x.classList.add('hidden'));
  }));
  if (isIOS && !isStandalone && iosHint) iosHint.classList.remove('hidden');

  // ------------------------------------------------------------ notifications
  const supported = PUSH_AVAILABLE && 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;

  function label(state) {
    return { on: '🔔 Alerts on', off: '🔕 Turn on alerts', denied: '🚫 Alerts blocked', busy: '…' }[state];
  }
  function paint(state) {
    pushBtns.forEach((b) => { b.textContent = label(state); b.dataset.state = state; b.classList.remove('hidden'); });
  }
  async function currentSubscription() {
    await swReady;
    const reg = registration || (await navigator.serviceWorker.ready);
    return reg.pushManager.getSubscription();
  }
  async function refresh() {
    if (!supported) return;
    if (Notification.permission === 'denied') { paint('denied'); return; }
    const sub = await currentSubscription().catch(() => null);
    paint(sub ? 'on' : 'off');
    if (!sub && Notification.permission === 'default' && body.dataset.role === 'owner') {
      showStrip('Turn on alerts to get price-approval requests on this phone.', 'push');
    }
  }
  async function enable() {
    paint('busy');
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') { paint(perm === 'denied' ? 'denied' : 'off'); return; }
    const status = await fetch('/push/status/', { credentials: 'same-origin' }).then((r) => r.json());
    if (!status.enabled) { alert('Notifications are not set up on the server yet.'); paint('off'); return; }
    await swReady;
    const reg = registration || (await navigator.serviceWorker.ready);
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(status.public_key) });
    }
    const res = await post('/push/subscribe/', sub.toJSON());
    if (!res.ok) { alert(res.error || 'Could not turn on alerts.'); paint('off'); return; }
    paint('on');
    if (strip) strip.classList.add('hidden');
    await post('/push/test/');
  }
  async function disable() {
    paint('busy');
    const sub = await currentSubscription().catch(() => null);
    if (sub) { await post('/push/unsubscribe/', { endpoint: sub.endpoint }); await sub.unsubscribe(); }
    paint('off');
  }
  pushBtns.forEach((b) => b.addEventListener('click', async () => {
    const state = b.dataset.state;
    try {
      if (state === 'on') { if (confirm('Stop alerts on this device?')) await disable(); }
      else if (state === 'denied') alert('Alerts are blocked for this app. Open your phone/browser settings for this site and allow notifications.');
      else await enable();
    } catch (err) { alert('Could not change alerts: ' + (err && err.message ? err.message : err)); refresh(); }
  }));
  if (supported) {
    // Always re-send the current subscription: keeps the server in step if the phone renewed it.
    refresh().then(async () => {
      const sub = await currentSubscription().catch(() => null);
      if (sub && Notification.permission === 'granted') post('/push/subscribe/', sub.toJSON());
    });
  } else if (PUSH_AVAILABLE && isIOS && !isStandalone) {
    // iPhones only allow notifications for apps added to the Home Screen.
    showStrip('To get alerts on iPhone, add this app to your Home Screen first (Share → Add to Home Screen).', 'ios');
  }
})();
