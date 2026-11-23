
"""Need Anything? — collaborative shopping server with PostgreSQL storage."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import json, secrets, time, os, threading, logging
import psycopg
from psycopg.rows import dict_row
from concurrent.futures import ThreadPoolExecutor

PUSH_POOL = ThreadPoolExecutor(max_workers=3)
ROOT = Path(__file__).resolve().parent
LOCK = threading.RLock()


def connect():
    url = os.environ.get('DATABASE_URL', '')
    if not url:
        raise RuntimeError('DATABASE_URL is missing from Render environment variables')
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=15)


def init():
    with connect() as db:
        db.execute('SELECT 1 FROM public.lists LIMIT 1')
        db.execute('SELECT 1 FROM public.subscriptions LIMIT 1')
        db.execute('SELECT 1 FROM public.list_members LIMIT 1')

def token():
    return secrets.token_urlsafe(24)


def send_push(list_id, initiator_endpoint, message, url):
    private = os.environ.get('VAPID_PRIVATE_KEY', '')
    email = os.environ.get('VAPID_CONTACT_EMAIL', '')
    if not private or not email:
        return

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logging.warning('Install requirements.txt to enable push')
        return

    with connect() as db:
        rows = db.execute(
            'SELECT endpoint, subscription FROM subscriptions WHERE list_id=%s',
            (list_id,)
        ).fetchall()

    for row in rows:
        if row['endpoint'] == initiator_endpoint:
            continue
        try:
            webpush(
                subscription_info=json.loads(row['subscription']),
                data=json.dumps({
                    'title': 'Need Anything?',
                    'body': message,
                    'url': url
                }),
                vapid_private_key=private,
                vapid_claims={'sub': 'mailto:' + email},
                ttl=300
            )
        except WebPushException as exc:
            if getattr(getattr(exc, 'response', None), 'status_code', None) in (404, 410):
                with connect() as db:
                    db.execute(
                        'DELETE FROM subscriptions WHERE list_id=%s AND endpoint=%s',
                        (list_id, row['endpoint'])
                    )
            else:
                logging.warning('Push delivery failed: %s', type(exc).__name__)
        except Exception:
            logging.exception('Push delivery error')


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, format, *args):
        pass  # Do not log private invitation tokens.

    def reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get('Content-Length', '0'))
        if n <= 0 or n > 10000:
            raise ValueError('Invalid request size')
        return json.loads(self.rfile.read(n))

    def do_GET(self):
        u = urlparse(self.path)

        if u.path == '/api/list':
            q = parse_qs(u.query)
            lid = q.get('id', [''])[0]
            key = q.get('key', [''])[0]

            with LOCK, connect() as db:
                row = db.execute(
                    'SELECT secret, data, updated FROM lists WHERE id=%s',
                    (lid,)
                ).fetchone()

            if not row or not secrets.compare_digest(row['secret'], key):
                return self.reply(404, {'error': 'List not found or invitation invalid'})

            return self.reply(200, {
                'list': json.loads(row['data']),
                'version': row['updated']
            })
        if u.path == '/api/members':
            q = parse_qs(u.query)
            lid = q.get('id', [''])[0]
            key = q.get('key', [''])[0]

            with LOCK, connect() as db:
                row = db.execute(
                    'SELECT secret FROM lists WHERE id=%s',
                    (lid,)
                ).fetchone()

                if not row or not secrets.compare_digest(row['secret'], key):
                    return self.reply(404, {'error': 'Invalid invitation'})

                members = db.execute(
                    '''SELECT member_id, name, last_active
                       FROM list_members
                       WHERE list_id=%s
                       ORDER BY name''',
                    (lid,)
                ).fetchall()

            now = time.time()

            return self.reply(200, {
                'members': [
                    {
                        'id': m['member_id'],
                        'name': m['name'],
                        'lastActive': m['last_active'],
                        'online': now - m['last_active'] < 45
                    }
                    for m in members
                ]
            })
        if u.path == '/api/health':
            return self.reply(200, {'ok': True})

        if u.path == '/api/push-config':
            return self.reply(200, {
                'publicKey': os.environ.get('VAPID_PUBLIC_KEY', '')
            })

       

        public_files = {
            '/', '/index.html', '/sw.js',
            '/manifest.webmanifest', '/icon.svg',
            '/needanything-logo.png'
        }
        if u.path not in public_files:
            return self.reply(404, {'error': 'Not found'})

        self.path = '/index.html' if u.path == '/' else u.path
        return super().do_GET()


    def do_POST(self):
        u = urlparse(self.path)
        try:
            data = self.read_json()
            if u.path == '/api/member':
                lid = str(data.get('id', ''))
                key = str(data.get('key', ''))
                member_id = str(data.get('memberId', ''))[:100]
                name = str(data.get('name', '')).strip()[:40]

                if not member_id or not name:
                    return self.reply(400, {'error': 'Member details required'})

                with LOCK, connect() as db:
                    row = db.execute(
                        'SELECT secret FROM lists WHERE id=%s',
                        (lid,)
                    ).fetchone()

                    if not row or not secrets.compare_digest(row['secret'], key):
                        return self.reply(404, {'error': 'Invalid invitation'})

                    db.execute(
                        '''INSERT INTO list_members
                           (list_id, member_id, name, last_active)
                           VALUES (%s, %s, %s, %s)
                           ON CONFLICT (list_id, member_id)
                           DO UPDATE SET
                           name=EXCLUDED.name,
                           last_active=EXCLUDED.last_active''',
                        (lid, member_id, name, time.time())
                    )

                return self.reply(200, {'ok': True})
            if u.path == '/api/create':
                name = str(data.get('name', '')).strip()[:60]
                if not name:
                    return self.reply(400, {'error': 'List name required'})

                lid = token()
                key = token()
                obj = {
                    'id': lid,
                    'name': name,
                    'items': [],
                    'shopping': None,
                    'events': []
                }

                with LOCK, connect() as db:
                    db.execute(
                        'INSERT INTO lists (id, secret, data, updated) VALUES (%s,%s,%s,%s)',
                        (lid, key, json.dumps(obj), time.time())
                    )

                return self.reply(200, {
                    'id': lid,
                    'key': key,
                    'list': obj
                })

            if u.path == '/api/subscribe':
                lid = str(data.get('id', ''))
                key = str(data.get('key', ''))
                sub = data.get('subscription')

                valid = (
                    isinstance(sub, dict)
                    and isinstance(sub.get('endpoint'), str)
                    and sub['endpoint'].startswith('https://')
                    and len(json.dumps(sub)) <= 8000
                    and isinstance(sub.get('keys'), dict)
                    and all(
                        isinstance(sub['keys'].get(k), str)
                        for k in ('auth', 'p256dh')
                    )
                )
                if not valid:
                    return self.reply(400, {'error': 'Invalid subscription'})

                with LOCK, connect() as db:
                    row = db.execute(
                        'SELECT secret FROM lists WHERE id=%s',
                        (lid,)
                    ).fetchone()

                    if not row or not secrets.compare_digest(row['secret'], key):
                        return self.reply(404, {'error': 'Invalid invitation'})

                    db.execute(
                        '''INSERT INTO subscriptions
                           (list_id, endpoint, subscription)
                           VALUES (%s,%s,%s)
                           ON CONFLICT (list_id, endpoint)
                           DO UPDATE SET subscription=EXCLUDED.subscription''',
                        (lid, sub['endpoint'], json.dumps(sub))
                    )

                return self.reply(200, {'ok': True})

            if u.path == '/api/unsubscribe':
                lid = str(data.get('id', ''))
                key = str(data.get('key', ''))
                endpoint = str(data.get('endpoint', ''))

                with LOCK, connect() as db:
                    row = db.execute(
                        'SELECT secret FROM lists WHERE id=%s',
                        (lid,)
                    ).fetchone()

                    if not row or not secrets.compare_digest(row['secret'], key):
                        return self.reply(404, {'error': 'Invalid invitation'})

                    db.execute(
                        'DELETE FROM subscriptions WHERE list_id=%s AND endpoint=%s',
                        (lid, endpoint)
                    )

                return self.reply(200, {'ok': True})

            if u.path == '/api/action':
                lid = str(data.get('id', ''))
                key = str(data.get('key', ''))
                action = data.get('action')
                person = str(data.get('person', 'Someone')).strip()[:40] or 'Someone'

                with LOCK, connect() as db:
                    row = db.execute(
                        'SELECT secret, data FROM lists WHERE id=%s FOR UPDATE',
                        (lid,)
                    ).fetchone()

                    if not row or not secrets.compare_digest(row['secret'], key):
                        return self.reply(404, {'error': 'Invalid invitation'})

                    obj = json.loads(row['data'])
                    items = obj['items']
                    shop = obj['shopping']
                    now = time.time()

                    if action == 'add':
                        name = str(data.get('name', '')).strip()[:100]
                        qty = str(data.get('qty', '')).strip()[:20]
                        if not name or len(items) >= 300:
                            return self.reply(400, {'error': 'Item missing or list full'})

                        items.insert(0, {
                            'id': token(),
                            'name': name,
                            'qty': qty,
                            'done': False,
                            'addedDuring': shop['id'] if shop else None
                        })
                        obj['events'].append({
                            'id': token(),
                            'type': 'add',
                            'by': person,
                            'at': now
                        })
                    elif action == 'reorder':
                        order = data.get('order')

                        if (
                            not isinstance(order, list)
                            or len(order) != len(items)
                            or not all(isinstance(x, str) for x in order)
                            or len(set(order)) != len(order)
                            or set(order) != {x['id'] for x in items}
                        ):
                            return self.reply(400, {'error': 'Invalid item order'})

                        by_id = {x['id']: x for x in items}
                        obj['items'] = [by_id[item_id] for item_id in order]


                    elif action == 'check':
                        item = next(
                            (x for x in items if x['id'] == data.get('itemId')),
                            None
                        )
                        if not item:
                            return self.reply(404, {'error': 'Item not found'})
                        item['done'] = bool(data.get('done'))

                    elif action == 'remove':
                        obj['items'] = [
                            x for x in items if x['id'] != data.get('itemId')
                        ]

                    elif action == 'start':
                        if shop:
                            return self.reply(409, {'error': 'Someone is already shopping'})

                        obj['shopping'] = {
                            'id': token(),
                            'by': person,
                            'lastCall': False
                        }
                        obj['events'].append({
                            'id': token(),
                            'type': 'start',
                            'by': person,
                            'at': now
                        })

                    elif action == 'lastCall':
                        if not shop:
                            return self.reply(409, {'error': 'No active shopping trip'})
                        if shop['lastCall']:
                            return self.reply(409, {'error': 'Last Call already sent'})

                        shop['lastCall'] = True
                        obj['events'].append({
                            'id': token(),
                            'type': 'lastCall',
                            'by': person,
                            'at': now
                        })

                    elif action == 'done':
                        if not shop:
                            return self.reply(409, {'error': 'No active shopping trip'})

                        obj['shopping'] = None
                        obj['events'].append({
                            'id': token(),
                            'type': 'done',
                            'by': person,
                            'at': now
                        })

                    elif action == 'rename':
                        name = str(data.get('name', '')).strip()[:60]
                        if not name:
                            return self.reply(400, {'error': 'Name required'})
                        obj['name'] = name

                    else:
                        return self.reply(400, {'error': 'Unknown action'})

                    obj['events'] = obj['events'][-30:]
                    db.execute(
                        'UPDATE lists SET data=%s, updated=%s WHERE id=%s',
                        (json.dumps(obj), now, lid)
                    )

                if action in ('start', 'lastCall'):
                    message = (
                        f'{person} is shopping! Need anything?'
                        if action == 'start'
                        else f'{person} is almost finished! Anything else?'
                    )
                    PUSH_POOL.submit(
                        send_push,
                        lid,
                        str(data.get('senderEndpoint', '')),
                        message,
                        '/?list=' + lid + '&key=' + key
                    )

                return self.reply(200, {'list': obj, 'version': now})

            return self.reply(404, {'error': 'Not found'})

        except (ValueError, TypeError, json.JSONDecodeError):
            return self.reply(400, {'error': 'Invalid request'})
        except Exception:
            logging.exception('Request failed')
            return self.reply(500, {'error': 'Server error'})


if __name__ == '__main__':
    init()
    port = int(os.environ.get('PORT', '8000'))
    print(f'Need Anything? running on port {port}', flush=True)
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()
