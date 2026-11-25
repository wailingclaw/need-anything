
"""Need Anything? — collaborative shopping server with PostgreSQL storage."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import json, secrets, time, os, threading, logging
import io
import base64
import requests
from PIL import Image, ImageOps
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from concurrent.futures import ThreadPoolExecutor

PUSH_POOL = ThreadPoolExecutor(max_workers=3)
ROOT = Path(__file__).resolve().parent
LOCK = threading.RLock()


DB_POOL = None


def init_pool():
    global DB_POOL

    url = os.environ.get('DATABASE_URL', '')

    if not url:
        raise RuntimeError(
            'DATABASE_URL is missing from Render environment variables'
        )

    DB_POOL = ConnectionPool(
        conninfo=url,
        min_size=1,
        max_size=3,
        timeout=15,
        kwargs={
            'row_factory': dict_row,
            'connect_timeout': 15
        },
        open=False
    )

    DB_POOL.open()
    DB_POOL.wait(timeout=30)


def connect():
    return DB_POOL.connection()


def init():
    with connect() as db:
        db.execute('SELECT 1 FROM public.lists LIMIT 1')
        db.execute('SELECT 1 FROM public.subscriptions LIMIT 1')
        db.execute('SELECT 1 FROM public.list_members LIMIT 1')

def token():
    return secrets.token_urlsafe(24)


def send_push(
    list_id,
    initiator_endpoint,
    message,
    url,
    message_es=None,
    target_member_id=None
):
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
            subscription = json.loads(row['subscription'])

            recipient_member_id = subscription.pop('memberId', '')

            if (
                target_member_id is not None
                and recipient_member_id != target_member_id
            ):
                continue

            recipient_language = subscription.pop('language', 'en')

            body = (
                message_es
                if recipient_language == 'es' and message_es
                else message
            )

            webpush(
                subscription_info=subscription,
                data=json.dumps({
                    'title': 'Need Anything?',
                    'body': body,
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

# Item photo storage
PHOTO_BUCKET = 'item-photos'
MAX_PHOTO_BYTES = 200_000

def storage_request(method, path, **kwargs):
    url = os.environ['SUPABASE_URL'].rstrip('/')
    key = os.environ['SUPABASE_SERVICE_KEY']

    headers = {
        'apikey': key
    }

    if not key.startswith('sb_secret_'):
        headers['Authorization'] = 'Bearer ' + key

    headers.update(kwargs.pop('headers', {}))

    response = requests.request(
        method,
        url + '/storage/v1/' + path,
        headers=headers,
        timeout=(5, 20),
        allow_redirects=False,
        **kwargs
    )

    if not response.ok:
        raise RuntimeError('Photo storage request failed')

    return response


def prepare_photo(encoded):
    if not isinstance(encoded, str):
        raise ValueError('Invalid photo')

    if len(encoded) > 400_000:
        raise ValueError('Photo too large')

    raw = base64.b64decode(encoded, validate=True)

    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > 4_000_000:
                raise ValueError('Photo dimensions too large')

            image = ImageOps.exif_transpose(image)
            image.thumbnail((1200, 1200))
            image = image.convert('RGB')

            for quality in (85, 75, 65, 50, 35):
                output = io.BytesIO()

                image.save(
                    output,
                    format='JPEG',
                    quality=quality,
                    optimize=True
                )

                result = output.getvalue()

                if len(result) <= MAX_PHOTO_BYTES:
                    return result

    except (OSError, Image.DecompressionBombError):
        raise ValueError('Invalid image')

    raise ValueError('Photo could not be compressed')


def photo_path(lid, photo_id):
    return f'{lid}/{photo_id}.jpg'


def upload_photo(lid, content):
    photo_id = token()

    storage_request(
        'POST',
        f'object/{PHOTO_BUCKET}/{photo_path(lid, photo_id)}',
        data=content,
        headers={
            'Content-Type': 'image/jpeg',
            'x-upsert': 'false'
        }
    )

    return photo_id


def delete_photo(lid, photo_id):
    if not photo_id:
        return

    try:
        storage_request(
            'DELETE',
            f'object/{PHOTO_BUCKET}',
            json={
                'prefixes': [photo_path(lid, photo_id)]
            }
        )

    except Exception:
        logging.warning('Photo cleanup failed')
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
        if u.path == '/api/photo':
            q = parse_qs(u.query)

            lid = q.get('id', [''])[0]
            key = q.get('key', [''])[0]
            item_id = q.get('itemId', [''])[0]
            photo_id = q.get('photo', [''])[0]

            with connect() as db:
                row = db.execute(
                    'SELECT secret, data FROM lists WHERE id=%s',
                    (lid,)
                ).fetchone()

            if not row or not secrets.compare_digest(
                row['secret'], key
            ):
                return self.reply(404, {
                    'error': 'Invalid invitation'
                })

            obj = json.loads(row['data'])

            item = next(
                (
                    x for x in obj['items']
                    if x['id'] == item_id
                ),
                None
            )

            if (
                not item
                or not photo_id
                or item.get('photo') != photo_id
            ):
                return self.reply(404, {
                    'error': 'Photo not found'
                })

            try:
                response = storage_request(
                    'GET',
                    f'object/authenticated/{PHOTO_BUCKET}/'
                    + photo_path(lid, photo_id)
                )

                content = response.content

                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header(
                    'Content-Length',
                    str(len(content))
                )
                self.end_headers()
                self.wfile.write(content)
                return

            except Exception:
                return self.reply(502, {
                    'error': 'Could not load photo'
                })
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
            if u.path == '/api/photo':
                n = int(
                    self.headers.get('Content-Length', '0')
                )

                if n <= 0 or n > 410_000:
                    return self.reply(413, {
                        'error': 'Photo request too large'
                    })

                data = json.loads(self.rfile.read(n))

                lid = str(data.get('id', ''))
                key = str(data.get('key', ''))
                item_id = str(data.get('itemId', ''))
                encoded = data.get('image')

                new_photo = None
                old_photo = None

                try:
                    with LOCK, connect() as db:
                        row = db.execute(
                            'SELECT secret, data FROM lists '
                            'WHERE id=%s FOR UPDATE',
                            (lid,)
                        ).fetchone()

                        if not row or not secrets.compare_digest(
                            row['secret'], key
                        ):
                            return self.reply(404, {
                                'error': 'Invalid invitation'
                            })

                        obj = json.loads(row['data'])

                        item = next(
                            (
                                x for x in obj['items']
                                if x['id'] == item_id
                            ),
                            None
                        )

                        if not item:
                            return self.reply(404, {
                                'error': 'Item not found'
                            })

                        old_photo = item.get('photo')

                        if encoded is not None:
                            photo_count = sum(
                                bool(x.get('photo'))
                                for x in obj['items']
                            )

                            if not old_photo and photo_count >= 50:
                                return self.reply(400, {
                                    'error': 'List photo limit reached'
                                })

                            content = prepare_photo(encoded)

                            new_photo = upload_photo(
                                lid, content
                            )

                        item['photo'] = new_photo

                        now = time.time()

                        obj['events'].append({
                            'id': token(),
                            'type': 'photo',
                            'by': str(
                                data.get('person', 'Someone')
                            )[:40],
                            'memberId': str(
                                data.get('memberId', '')
                            )[:100],
                            'at': now
                        })

                        obj['events'] = obj['events'][-100:]

                        db.execute(
                            'UPDATE lists SET data=%s, '
                            'updated=%s WHERE id=%s',
                            (json.dumps(obj), now, lid)
                        )

                except Exception:
                    if new_photo:
                        delete_photo(lid, new_photo)
                    raise

                if old_photo:
                    delete_photo(lid, old_photo)

                return self.reply(200, {
                    'list': obj,
                    'version': now
                })
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
                    'lastTrip': None,
                    'tripHistory': [],
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
                sub = dict(sub)

                sub['language'] = (
                    'es' if data.get('language') == 'es'
                    else 'en'
                )

                sub['memberId'] = str(
                    data.get('memberId', '')
                )[:100]
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
            if u.path == '/api/leave':
                lid = str(data.get('id', ''))
                key = str(data.get('key', ''))
                member_id = str(data.get('memberId', ''))[:100]
                endpoint = str(data.get('endpoint', ''))

                if not member_id:
                    return self.reply(400, {
                        'error': 'Member ID required'
                    })

                with LOCK, connect() as db:
                    row = db.execute(
                        'SELECT secret FROM lists WHERE id=%s',
                        (lid,)
                    ).fetchone()

                    if not row or not secrets.compare_digest(
                        row['secret'], key
                    ):
                        return self.reply(404, {
                            'error': 'Invalid invitation'
                        })

                    db.execute(
                        '''DELETE FROM list_members
                           WHERE list_id=%s AND member_id=%s''',
                        (lid, member_id)
                    )

                    if endpoint:
                        db.execute(
                            '''DELETE FROM subscriptions
                               WHERE list_id=%s AND endpoint=%s''',
                            (lid, endpoint)
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
                removed_photo = None
                person = str(data.get('person', 'Someone')).strip()[:40] or 'Someone'
                member_id = str(data.get('memberId', ''))[:100]
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

                        # Allow previously checked items to
                        # count when collected again during a trip.
                        if shop and not item['done']:
                            if isinstance(shop.get('previouslyDone'), list):
                                shop['previouslyDone'] = [
                                    item_id
                                    for item_id in shop['previouslyDone']
                                    if item_id != item['id']
                                ]

                    elif action == 'remove':
                        removed_item = next(
                            (
                                x for x in items
                                if x['id'] == data.get('itemId')
                            ),
                            None
                        )

                        if removed_item:
                            removed_photo = removed_item.get('photo')

                        obj['items'] = [
                            x for x in items
                            if x['id'] != data.get('itemId')
                        ]

                    elif action == 'start':
                        if shop:
                            return self.reply(409, {'error': 'Someone is already shopping'})

                        obj['shopping'] = {
                            'id': token(),
                            'by': person,
                            'memberId': member_id,
                            'lastCall': False,
                            'startedAt': now,
                            'previouslyDone': [
                                item['id']
                                for item in items
                                if item.get('done')
                            ]
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
                        shop['lastCallAt'] = now
                        shop['lastCallResponses'] = {}
                        obj['events'].append({
                            'id': token(),
                            'type': 'lastCall',
                            'by': person,
                            'at': now
                        })
                    elif action == 'lastCallResponse':
                        if not shop or not shop.get('lastCall'):
                            return self.reply(409, {
                                'error': 'No active Last Call'
                            })

                        if data.get('tripId') != shop['id']:
                            return self.reply(409, {
                                'error': 'Shopping trip has changed'
                            })

                        if not member_id:
                            return self.reply(400, {
                                'error': 'Member ID required'
                            })

                        if member_id == shop.get('memberId'):
                            return self.reply(400, {
                                'error': 'Shopper cannot respond to own Last Call'
                            })

                        member = db.execute(
                            'SELECT name FROM list_members '
                            'WHERE list_id=%s AND member_id=%s',
                            (lid, member_id)
                        ).fetchone()

                        if not member:
                            return self.reply(403, {
                                'error': 'List member not found'
                            })

                        response = data.get('response')

                        if response not in ('good', 'adding'):
                            return self.reply(400, {
                                'error': 'Invalid Last Call response'
                            })

                        responses = shop.setdefault(
                            'lastCallResponses', {}
                        )

                        previous = responses.get(member_id, {})

                        if previous.get('response') == response:
                            return self.reply(200, {
                                'list': obj,
                                'version': now
                            })

                        responses[member_id] = {
                            'name': member['name'],
                            'response': response,
                            'at': now
                        }
                    elif action == 'done':
                        if not shop:
                            return self.reply(409, {'error': 'No active shopping trip'})

                        started_at = shop.get('startedAt')

                        # Calculate shopping duration
                        duration = None

                        if isinstance(started_at, (int, float)):
                            duration = max(
                                0,
                                int(now - started_at)
                            )
                        # Calculate items collected during this trip
                        previously_done = set(
                            shop.get('previouslyDone', [])
                        )

                        collected_products = [
                            {
                                'id': item['id'],
                                'name': item['name'],
                                'qty': item.get('qty', '')
                            }
                            for item in items
                            if item.get('done')
                            and item['id'] not in previously_done
                        ]

                        collected_items = [
                            item['id']
                            for item in collected_products
                        ]

                        remaining_items = sum(
                            1 for item in items
                            if not item.get('done')
                        )
                        # Remember the previous completed trip
                        previous_trip = obj.get('lastTrip')

                        # Preserve the completed shopping trip
                        obj['lastTrip'] = {
                            'id': shop['id'],
                            'by': shop.get('by', person),
                            'memberId': shop.get('memberId', ''),
                            'startedAt': started_at,
                            'finishedAt': now,
                            'durationSeconds': duration,
                            'itemsCollected': len(collected_items),
                            'collectedItemIds': collected_items,
                            'collectedProducts': (
                                collected_products
                                if 'previouslyDone' in shop
                                else None
                            ),
                            'itemsRemaining': remaining_items,
                            'itemCountAccurate': 'previouslyDone' in shop,
                            'finishedBy': person,
                            'finishedByMemberId': member_id
                        }
                        # Preserve shopping history
                        history = obj.get('tripHistory', [])

                        if not isinstance(history, list):
                            history = []
                        # Preserve the latest trip from Stage 4B
                        if previous_trip and not any(
                            trip.get('id') == previous_trip.get('id')
                            for trip in history
                        ):
                            history.insert(0, previous_trip)
                        # Prevent duplicate trip records
                        history = [
                            trip for trip in history
                            if trip.get('id') != obj['lastTrip']['id']
                        ]

                        # Store the newest trip first
                        history.insert(0, obj['lastTrip'])

                        # Retain the 100 most recent trips
                        obj['tripHistory'] = history[:100]
                        # Close the active shopping session
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

                    if action in ('check', 'remove', 'reorder', 'rename'):
                        obj['events'].append({
                            'id': token(),
                            'type': action,
                            'by': person,
                            'at': now
                        })

                    if obj['events']:
                        latest_event = obj['events'][-1]

                        if latest_event.get('at') == now:
                            latest_event['memberId'] = member_id

                    obj['events'] = obj['events'][-100:]
                    db.execute(
                        'UPDATE lists SET data=%s, updated=%s WHERE id=%s',
                        (json.dumps(obj), now, lid)
                    )
                if removed_photo:
                    delete_photo(lid, removed_photo)
                if action in ('start', 'lastCall'):
                    message = (
                        f'{person} is shopping! Need anything?'
                        if action == 'start'
                        else f'{person} is almost finished! Anything else?'
                    )

                    message_es = (
                        f'¡{person} está de compras! ¿Necesitas algo?'
                        if action == 'start'
                        else f'¡{person} está por terminar! ¿Algo más?'
                    )

                    PUSH_POOL.submit(
                        send_push,
                        lid,
                        str(data.get('senderEndpoint', '')),
                        message,
                        '/?list=' + lid + '&key=' + key,
                        message_es
                    )

                if action == 'lastCallResponse':
                    shopper_id = shop.get('memberId')

                    if shopper_id:
                        responder = responses[member_id]['name']

                        message = (
                            f"{responder}: I'm good, thank you!"
                            if response == 'good'
                            else f'{responder} is adding something!'
                        )

                        message_es = (
                            f'{responder}: ¡No necesito nada más, gracias!'
                            if response == 'good'
                            else f'¡{responder} está agregando algo!'
                        )

                        PUSH_POOL.submit(
                            send_push,
                            lid,
                            str(data.get('senderEndpoint', '')),
                            message,
                            '/?list=' + lid + '&key=' + key,
                            message_es,
                            shopper_id
                        )

                return self.reply(200, {'list': obj, 'version': now})

            return self.reply(404, {'error': 'Not found'})

        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            logging.warning(
                'Invalid request on %s: %s',
                u.path,
                type(exc).__name__
            )
            return self.reply(400, {'error': 'Invalid request'})
        except Exception:
            logging.exception('Request failed')
            return self.reply(500, {'error': 'Server error'})


if __name__ == '__main__':
    init_pool()
    init()
    port = int(os.environ.get('PORT', '8000'))
    print(f'Need Anything? running on port {port}', flush=True)
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()
