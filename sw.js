self.addEventListener('push', event => {
  let data = {};

  try {
    data = event.data?.json() || {};
  } catch {}

  event.waitUntil(
    self.registration.showNotification(
      data.title || 'Need Anything?',
      {
        body: data.body || 'Your list has an update',
        icon: '/needanything-logo.png',
        badge: '/icon.svg',
        data: {
          url: data.url || '/'
        },
        tag: 'need-anything-' + Date.now()
      }
    )
  );
});

self.addEventListener('notificationclick', event => {
  event.notification.close();

  const url = new URL(
    event.notification.data?.url || '/',
    self.location.origin
  );

  if (url.origin !== self.location.origin) return;

  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({
      type: 'window',
      includeUncontrolled: true
    });

    for (const client of windows) {
      if (new URL(client.url).origin === url.origin) {
        try {
          const navigated = await client.navigate(url.href);

          if (navigated) {
            await navigated.focus();
            return;
          }
        } catch (error) {
          console.warn('Could not navigate existing window', error);
        }
      }
    }

    await self.clients.openWindow(url.href);
  })());
});

self.addEventListener('install', event => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', event => {
  event.waitUntil(self.clients.claim());
});
