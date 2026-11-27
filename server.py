
"""Need Anything? — collaborative shopping server with PostgreSQL storage."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import json, secrets, time, os, threading, logging
import hashlib
import re
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from argon2 import PasswordHasher
from argon2.exceptions import (
    VerifyMismatchError,
    VerificationError,
    InvalidHashError
)
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
# Private identity configuration

IDENTITY_ENABLED = (
    os.environ.get('IDENTITY_ENABLED', 'false').lower()
    == 'true'
)

IDENTITY_COOKIE = '__Host-na_identity'

IDENTITY_SESSION_DAYS = 30
IDENTITY_MAX_SESSIONS = 10
# Server resource protection

REQUEST_TIMEOUT_SECONDS = 30
MAX_CONCURRENT_REQUESTS = 24

REQUEST_SLOTS = threading.BoundedSemaphore(
    MAX_CONCURRENT_REQUESTS
)
IDENTITY_ORIGIN = os.environ.get(
    'IDENTITY_ORIGIN',
    'https://need-anything.onrender.com'
).rstrip('/')
IDENTITY_USERNAME_PATTERN = re.compile(
    r'^[a-z0-9_]{3,24}$'
)

IDENTITY_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=19456,
    parallelism=1,
    hash_len=32,
    salt_len=16
)

IDENTITY_HASH_SLOTS = threading.BoundedSemaphore(2)
# Dummy password hash for login timing protection

IDENTITY_DUMMY_HASH = (
    IDENTITY_PASSWORD_HASHER.hash(
        secrets.token_urlsafe(32)
    )
    if IDENTITY_ENABLED else None
)
IDENTITY_REGISTRATION_ENABLED = (
    os.environ.get(
        'IDENTITY_REGISTRATION_ENABLED', 'false'
    ).lower() == 'true'
)

IDENTITY_RATE_LOCK = threading.Lock()
IDENTITY_RATE_BUCKETS = {}
IDENTITY_RATE_MAX_BUCKETS = 5000
# Private identity emblem collections

IDENTITY_EMBLEMS = {
    'initials',

    # Nature and outdoors
    'mountain', 'trees', 'leaf', 'flower-2',
    'sprout', 'waves', 'tent', 'compass',

    # Sky and elements
    'sun', 'moon', 'star', 'cloud',
    'snowflake', 'flame', 'zap', 'rainbow',

    # Animals
    'paw-print', 'dog', 'cat', 'bird',
    'fish', 'rabbit', 'turtle', 'squirrel',

    # Food and culinary
    'chef-hat', 'cooking-pot', 'utensils',
    'sandwich', 'coffee', 'pizza', 'cherry',
    'ice-cream-cone', 'apple', 'cookie',
    'cupcake', 'croissant',

    # Creativity and entertainment
    'palette', 'music', 'headphones',
    'gamepad-2', 'camera', 'book-open',
    'guitar', 'pencil',

    # Adventure and movement
    'bike', 'footprints', 'sailboat', 'plane',
    'rocket', 'trophy', 'dumbbell', 'flag',

    # Meaning and personality
    'heart', 'peace', 'infinity', 'gem',
    'anchor', 'key', 'shield', 'sparkles',

    # Fun and quirky
    'smile', 'ghost', 'bot', 'crown',
    'glasses', 'umbrella', 'balloon', 'gift'
}

IDENTITY_EMBLEM_COLORS = {
    'nature': {
        'meadow', 'forest', 'ocean', 'glacier',
        'sand', 'sunset', 'lavender', 'stone'
    },

    'pastel': {
        'lime', 'sky', 'peach', 'blush',
        'lilac', 'mint', 'sunshine', 'periwinkle'
    },

    'metallic': {
        'gold', 'platinum', 'rose-gold',
        'copper', 'titanium', 'emerald'
    }
}


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
        if IDENTITY_ENABLED:
            db.execute('SELECT 1 FROM public.identities LIMIT 1')
            db.execute('SELECT 1 FROM public.identity_sessions LIMIT 1')
            db.execute('SELECT 1 FROM public.identity_recovery LIMIT 1')
            db.execute('SELECT 1 FROM public.identity_rate_limits LIMIT 1')
def token():
    return secrets.token_urlsafe(24)
# Private identity helpers

def normalize_username(value):
    if not isinstance(value, str):
        raise ValueError('Invalid username')

    username = value.strip().lower()

    if username.startswith('@'):
        username = username[1:]

    if not IDENTITY_USERNAME_PATTERN.fullmatch(username):
        raise ValueError('Invalid username')

    return username


def validate_identity_password(password):
    if not isinstance(password, str):
        raise ValueError('Invalid password')

    if not 15 <= len(password) <= 128:
        raise ValueError(
            'Password must contain 15 to 128 characters'
        )

    try:
        encoded = password.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('Invalid password characters')

    if len(encoded) > 512:
        raise ValueError('Password too large')

    if '\x00' in password:
        raise ValueError('Invalid password characters')

    return password

def validate_display_name(value):
    if not isinstance(value, str):
        raise ValueError('Invalid display name')

    name = value.strip()

    if not 1 <= len(name) <= 40:
        raise ValueError('Invalid display name')

    if any(not char.isprintable() for char in name):
        raise ValueError('Invalid display name')

    return name
def identity_secret():
    return secrets.token_urlsafe(32)


def identity_secret_hash(value):
    return hashlib.sha256(
        value.encode('utf-8')
    ).hexdigest()


class IdentityBusyError(Exception):
    pass


def hash_identity_password(password):
    if not IDENTITY_HASH_SLOTS.acquire(blocking=False):
        raise IdentityBusyError()

    try:
        return IDENTITY_PASSWORD_HASHER.hash(password)
    finally:
        IDENTITY_HASH_SLOTS.release()


def verify_identity_password(password_hash, password):
    if not IDENTITY_HASH_SLOTS.acquire(blocking=False):
        raise IdentityBusyError()

    try:
        return IDENTITY_PASSWORD_HASHER.verify(
            password_hash,
            password
        )
    except (
        VerifyMismatchError,
        VerificationError,
        InvalidHashError
    ):
        return False
    finally:
        IDENTITY_HASH_SLOTS.release()
def public_identity(row):
    return {
        'id': str(row['id']),
        'username': row['username'],
        'displayName': row['display_name'],
        'emblem': row['emblem'],
        'emblemColor': row['emblem_color'],
        'emblemCollection': row['emblem_collection']
    }

# Private identity rate limiting

def identity_rate_allowed(key, limit, window):
    now = time.monotonic()

    with IDENTITY_RATE_LOCK:
        expired = [
            bucket_key
            for bucket_key, bucket in IDENTITY_RATE_BUCKETS.items()
            if bucket['expires'] <= now
        ]

        for bucket_key in expired:
            del IDENTITY_RATE_BUCKETS[bucket_key]

        bucket = IDENTITY_RATE_BUCKETS.get(key)

        if bucket is None:
            if len(IDENTITY_RATE_BUCKETS) >= IDENTITY_RATE_MAX_BUCKETS:
                return False

            bucket = {
                'count': 0,
                'expires': now + window
            }

            IDENTITY_RATE_BUCKETS[key] = bucket

        if bucket['count'] >= limit:
            return False

        bucket['count'] += 1

        return True

# Persistent identity rate limiting

def persistent_identity_rate_allowed(
    key, limit, window
):
    with connect() as db:

        # Periodically remove expired rate-limit records.
        if secrets.randbelow(100) == 0:
            db.execute(
                '''
                DELETE FROM public.identity_rate_limits
                WHERE expires_at <= NOW()
                '''
            )

        row = db.execute(
            '''
            INSERT INTO public.identity_rate_limits
                (bucket_key, request_count, expires_at)
            VALUES (
                %s,
                1,
                NOW() + (%s * INTERVAL '1 second')
            )
            ON CONFLICT (bucket_key)
            DO UPDATE SET
                request_count = CASE
                    WHEN identity_rate_limits.expires_at <= NOW()
                    THEN 1
                    ELSE identity_rate_limits.request_count + 1
                END,
                expires_at = CASE
                    WHEN identity_rate_limits.expires_at <= NOW()
                    THEN EXCLUDED.expires_at
                    ELSE identity_rate_limits.expires_at
                END
            WHERE identity_rate_limits.expires_at <= NOW()
                OR identity_rate_limits.request_count < %s
            RETURNING request_count
            ''',
            (key, window, limit)
        ).fetchone()

    return row is not None
# Private identity action limits

def identity_action_allowed(action, username):
    username = normalize_username(username)
    account_key = identity_secret_hash(username)

    if action == 'register':
        return persistent_identity_rate_allowed(
            'identity:register:global', 10, 3600
        )

    if action == 'login':
        return persistent_identity_rate_allowed(
            'identity:login:' + account_key,
            10, 300
        )

    if action == 'recover':
        return persistent_identity_rate_allowed(
            'identity:recover:' + account_key,
            5, 900
        )

    return False
# Private identity registration

def register_identity(username, display_name, password):
    username = normalize_username(username)
    display_name = validate_display_name(display_name)
    password = validate_identity_password(password)

    password_hash = hash_identity_password(password)

    recovery_key = identity_secret()
    recovery_hash = identity_secret_hash(recovery_key)

    with connect() as db:
        identity = db.execute(
            '''
            INSERT INTO public.identities
                (username, display_name, password_hash)
            VALUES (%s, %s, %s)
            ON CONFLICT (username) DO NOTHING
            RETURNING *
            ''',
            (username, display_name, password_hash)
        ).fetchone()

        if identity is None:
            return None

        db.execute(
            '''
            INSERT INTO public.identity_recovery
                (identity_id, recovery_hash)
            VALUES (%s, %s)
            ''',
            (identity['id'], recovery_hash)
        )

        raw_token = create_identity_session(
            db, identity['id']
        )

    return {
        'identity': public_identity(identity),
        'recoveryKey': recovery_key,
        'sessionToken': raw_token
    }
# Private identity sign-in

def authenticate_identity(username, password):
    username = normalize_username(username)
    password = validate_identity_password(password)

    with connect() as db:
        identity = db.execute(
            '''
            SELECT id, password_hash
            FROM public.identities
            WHERE username = %s
            ''',
            (username,)
        ).fetchone()

    if identity is None:
        verify_identity_password(
            IDENTITY_DUMMY_HASH,
            password
        )
        return None

    original_hash = identity['password_hash']

    if not verify_identity_password(
        original_hash, password
    ):
        return None

    with connect() as db:
        current = db.execute(
            '''
            SELECT *
            FROM public.identities
            WHERE id = %s
            FOR UPDATE
            ''',
            (identity['id'],)
        ).fetchone()

        if current is None:
            return None

        if not secrets.compare_digest(
            current['password_hash'],
            original_hash
        ):
            return None

        raw_token = create_identity_session(
            db, current['id']
        )

    return {
        'identity': public_identity(current),
        'sessionToken': raw_token
    }
# Private identity recovery

def recover_identity(username, recovery_key, new_password):
    username = normalize_username(username)
    new_password = validate_identity_password(new_password)

    if not isinstance(recovery_key, str):
        raise ValueError('Invalid recovery key')

    if not re.fullmatch(
        r'[A-Za-z0-9_-]{43}', recovery_key
    ):
        raise ValueError('Invalid recovery key')

    recovery_hash = identity_secret_hash(recovery_key)

    with connect() as db:
        identity = db.execute(
            '''
            SELECT i.id, r.recovery_hash
            FROM public.identities AS i
            JOIN public.identity_recovery AS r
                ON r.identity_id = i.id
            WHERE i.username = %s
            ''',
            (username,)
        ).fetchone()

    if identity is None:
        return None

    if not secrets.compare_digest(
        identity['recovery_hash'],
        recovery_hash
    ):
        return None

    password_hash = hash_identity_password(new_password)

    new_recovery_key = identity_secret()
    new_recovery_hash = identity_secret_hash(
        new_recovery_key
    )

    with connect() as db:
        current = db.execute(
            '''
            SELECT *
            FROM public.identities
            WHERE id = %s
            FOR UPDATE
            ''',
            (identity['id'],)
        ).fetchone()

        if current is None:
            return None

        recovery = db.execute(
            '''
            SELECT recovery_hash
            FROM public.identity_recovery
            WHERE identity_id = %s
            FOR UPDATE
            ''',
            (identity['id'],)
        ).fetchone()

        if recovery is None:
            return None

        if not secrets.compare_digest(
            recovery['recovery_hash'],
            recovery_hash
        ):
            return None

        updated = db.execute(
            '''
            UPDATE public.identities
            SET password_hash = %s,
                updated_at = NOW()
            WHERE id = %s
            RETURNING *
            ''',
            (password_hash, identity['id'])
        ).fetchone()

        db.execute(
            '''
            UPDATE public.identity_recovery
            SET recovery_hash = %s,
                created_at = NOW()
            WHERE identity_id = %s
            ''',
            (new_recovery_hash, identity['id'])
        )

        db.execute(
            '''
            UPDATE public.identity_sessions
            SET revoked_at = NOW()
            WHERE identity_id = %s
                AND revoked_at IS NULL
            ''',
            (identity['id'],)
        )

        raw_token = create_identity_session(
            db, identity['id']
        )

    return {
        'identity': public_identity(updated),
        'recoveryKey': new_recovery_key,
        'sessionToken': raw_token
    }

# Private identity personalization

def update_identity_profile(raw_token, data):
    display_name = validate_display_name(
        data.get('displayName')
    )

    emblem = data.get('emblem')
    collection = data.get('emblemCollection')
    color = data.get('emblemColor')

    discoverable = data.get('discoverable')
    allow_requests = data.get('allowRequests')

    if not isinstance(emblem, str):
        raise ValueError('Invalid emblem')

    if emblem not in IDENTITY_EMBLEMS:
        raise ValueError('Invalid emblem')

    if not isinstance(collection, str):
        raise ValueError('Invalid emblem collection')

    if collection not in IDENTITY_EMBLEM_COLORS:
        raise ValueError('Invalid emblem collection')

    if not isinstance(color, str):
        raise ValueError('Invalid emblem color')

    if color not in IDENTITY_EMBLEM_COLORS[collection]:
        raise ValueError('Invalid emblem color')

    if type(discoverable) is not bool:
        raise ValueError('Invalid privacy preference')

    if type(allow_requests) is not bool:
        raise ValueError('Invalid request preference')

    with connect() as db:
        identity = find_identity_session(
            db, raw_token
        )

        if identity is None:
            return None

        current = db.execute(
            '''
            SELECT *
            FROM public.identities
            WHERE id = %s
            FOR UPDATE
            ''',
            (identity['id'],)
        ).fetchone()

        if current is None:
            return None

        session = find_identity_session(
            db, raw_token
        )

        if session is None:
            return None

        if (
            collection == 'metallic'
            and not current['metallic_unlocked']
        ):
            raise PermissionError(
                'Metallic collection is locked'
            )

        updated = db.execute(
            '''
            UPDATE public.identities
            SET display_name = %s,
                emblem = %s,
                emblem_color = %s,
                emblem_collection = %s,
                discoverable = %s,
                allow_requests = %s,
                updated_at = NOW()
            WHERE id = %s
            RETURNING *
            ''',
            (
                display_name,
                emblem,
                color,
                collection,
                discoverable,
                allow_requests,
                identity['id']
            )
        ).fetchone()

    return {
        'identity': public_identity(updated),
        'metallicUnlocked': updated['metallic_unlocked'],
        'discoverable': updated['discoverable'],
        'allowRequests': updated['allow_requests']
    }


# Private identity deletion

def delete_identity(raw_token, password):
    password = validate_identity_password(password)

    with connect() as db:
        identity = find_identity_session(
            db, raw_token
        )

        if identity is None:
            return 'unauthorized'

        credentials = db.execute(
            '''
            SELECT password_hash
            FROM public.identities
            WHERE id = %s
            ''',
            (identity['id'],)
        ).fetchone()

    if credentials is None:
        return 'unauthorized'

    if not persistent_identity_rate_allowed(
        'identity:delete:account:'
        + str(identity['id']),
        5, 900
    ):
        return 'rate_limited'

    original_hash = credentials['password_hash']

    if not verify_identity_password(
        original_hash, password
    ):
        return 'invalid_password'

    with connect() as db:
        current = db.execute(
            '''
            SELECT password_hash
            FROM public.identities
            WHERE id = %s
            FOR UPDATE
            ''',
            (identity['id'],)
        ).fetchone()

        if current is None:
            return 'unauthorized'

        if not secrets.compare_digest(
            current['password_hash'],
            original_hash
        ):
            return 'invalid_password'

        session = find_identity_session(
            db, raw_token
        )

        if session is None:
            return 'unauthorized'

        db.execute(
            '''
            DELETE FROM public.identities
            WHERE id = %s
            ''',
            (identity['id'],)
        )

    return 'deleted'
# Private identity sessions

def create_identity_session(db, identity_id):
    # Remove expired and revoked sessions.
    db.execute(
        '''
        DELETE FROM public.identity_sessions
        WHERE identity_id = %s
            AND (
                expires_at <= NOW()
                OR revoked_at IS NOT NULL
            )
        ''',
        (identity_id,)
    )

    # Make room for the new session.
    db.execute(
        '''
        DELETE FROM public.identity_sessions
        WHERE id IN (
            SELECT id
            FROM public.identity_sessions
            WHERE identity_id = %s
            ORDER BY created_at DESC, id DESC
            OFFSET %s
        )
        ''',
        (
            identity_id,
            IDENTITY_MAX_SESSIONS - 1
        )
    )

    raw_token = identity_secret()
    token_hash = identity_secret_hash(raw_token)

    expires_at = (
        datetime.now(timezone.utc)
        + timedelta(days=IDENTITY_SESSION_DAYS)
    )

    db.execute(
        '''
        INSERT INTO public.identity_sessions
            (identity_id, token_hash, expires_at)
        VALUES (%s, %s, %s)
        ''',
        (identity_id, token_hash, expires_at)
    )

    return raw_token


def find_identity_session(db, raw_token):
    if not isinstance(raw_token, str):
        return None

    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', raw_token):
        return None

    token_hash = identity_secret_hash(raw_token)

    return db.execute(
        '''
        SELECT
            i.id,
            i.username,
            i.display_name,
            i.emblem,
            i.emblem_color,
            i.emblem_collection,
            i.metallic_unlocked,
            i.discoverable,
            i.allow_requests,
            s.id AS session_id
        FROM public.identity_sessions AS s
        JOIN public.identities AS i
            ON i.id = s.identity_id
        WHERE s.token_hash = %s
            AND s.revoked_at IS NULL
            AND s.expires_at > NOW()
        ''',
        (token_hash,)
    ).fetchone()


def revoke_identity_session(db, raw_token):
    if not isinstance(raw_token, str):
        return

    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', raw_token):
        return

    db.execute(
        '''
        UPDATE public.identity_sessions
        SET revoked_at = NOW()
        WHERE token_hash = %s
            AND revoked_at IS NULL
        ''',
        (identity_secret_hash(raw_token),)
    )


# Private identity session rotation

def rotate_previous_identity_session(
    previous_token, new_token
):
    if previous_token is None:
        return

    if secrets.compare_digest(
        previous_token, new_token
    ):
        return

    try:
        with connect() as db:
            revoke_identity_session(
                db, previous_token
            )

    except Exception:
        logging.error(
            'Previous identity session rotation failed'
        )
# Private identity cookies

def make_identity_cookie(raw_token):
    cookie = SimpleCookie()

    cookie[IDENTITY_COOKIE] = raw_token

    cookie[IDENTITY_COOKIE]['path'] = '/'
    cookie[IDENTITY_COOKIE]['secure'] = True
    cookie[IDENTITY_COOKIE]['httponly'] = True
    cookie[IDENTITY_COOKIE]['samesite'] = 'Strict'
    cookie[IDENTITY_COOKIE]['max-age'] = (
        IDENTITY_SESSION_DAYS * 86400
    )

    return cookie.output(header='').strip()


def clear_identity_cookie():
    cookie = SimpleCookie()

    cookie[IDENTITY_COOKIE] = ''

    cookie[IDENTITY_COOKIE]['path'] = '/'
    cookie[IDENTITY_COOKIE]['secure'] = True
    cookie[IDENTITY_COOKIE]['httponly'] = True
    cookie[IDENTITY_COOKIE]['samesite'] = 'Strict'
    cookie[IDENTITY_COOKIE]['max-age'] = 0

    return cookie.output(header='').strip()# Trusted browser push service providers

TRUSTED_PUSH_HOSTS = {
    'fcm.googleapis.com',
    'web.push.apple.com',
    'updates.push.services.mozilla.com'
}


def valid_push_endpoint(endpoint):
    if not isinstance(endpoint, str):
        return False

    if not 1 <= len(endpoint) <= 4096:
        return False

    if any(
        char.isspace() or ord(char) < 32
        for char in endpoint
    ):
        return False

    if '\\' in endpoint:
        return False

    try:
        parsed = urlparse(endpoint)
        host = parsed.hostname

        if (
            parsed.scheme != 'https'
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
            or not parsed.path.startswith('/')
        ):
            return False

        if host in TRUSTED_PUSH_HOSTS:
            return True

        if host.endswith('.notify.windows.com'):
            return True

        return False

    except ValueError:
        return False


class PushRequestSession(requests.Session):
    def request(self, method, url, **kwargs):
        if not valid_push_endpoint(url):
            raise ValueError('Untrusted push endpoint')

        kwargs['allow_redirects'] = False
        kwargs['timeout'] = (5, 10)

        return super().request(method, url, **kwargs)
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

            if not isinstance(subscription, dict):
                continue

            if not valid_push_endpoint(subscription.get('endpoint')):
                logging.warning('Untrusted push endpoint skipped')
                continue

            if subscription.get('endpoint') != row['endpoint']:
                logging.warning('Mismatched push endpoint skipped')
                continue

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

            with PushRequestSession() as push_session:
                webpush(
                    subscription_info=subscription,
                    data=json.dumps({
                        'title': 'Need Anything?',
                        'body': body,
                        'url': url
                    }),
                    vapid_private_key=private,
                    vapid_claims={'sub': 'mailto:' + email},
                    ttl=300,
                    timeout=10,
                    requests_session=push_session
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


# Protected HTTP server

class ProtectedHTTPServer(ThreadingHTTPServer):

    daemon_threads = True

    def process_request(
        self, request, client_address
    ):
        if not REQUEST_SLOTS.acquire(
            blocking=False
        ):
            self.shutdown_request(request)
            return

        try:
            request.settimeout(
                REQUEST_TIMEOUT_SECONDS
            )

            super().process_request(
                request, client_address
            )

        except BaseException:
            REQUEST_SLOTS.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(
        self, request, client_address
    ):
        try:
            super().process_request_thread(
                request, client_address
            )

        finally:
            REQUEST_SLOTS.release()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, format, *args):
        pass  # Do not log private invitation tokens.

    def reply(self, status, data, cookie=None):
        body = json.dumps(data).encode()

        self.send_response(status)

        self.send_header(
            'Content-Type',
            'application/json; charset=utf-8'
        )

        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')

        if cookie is not None:
            self.send_header('Set-Cookie', cookie)

        self.send_header(
            'Content-Length',
            str(len(body))
        )

        self.end_headers()
        self.wfile.write(body)
    def identity_session_token(self):
        raw_cookie = self.headers.get('Cookie', '')

        if not raw_cookie or len(raw_cookie) > 8192:
            return None

        try:
            cookie = SimpleCookie()
            cookie.load(raw_cookie)

            session = cookie.get(IDENTITY_COOKIE)

            if session is None:
                return None

            raw_token = session.value

            if not re.fullmatch(
                r'[A-Za-z0-9_-]{43}',
                raw_token
            ):
                return None

            return raw_token

        except Exception:
            return None


    def current_identity(self, db):
        raw_token = self.identity_session_token()

        if raw_token is None:
            return None

        return find_identity_session(db, raw_token)

    def valid_identity_request(self):
        origin = self.headers.get('Origin', '')

        if origin != IDENTITY_ORIGIN:
            return False

        content_type = (
            self.headers.get('Content-Type', '')
            .split(';', 1)[0]
            .strip()
            .lower()
        )

        return content_type == 'application/json'

    def read_identity_json(self):
        if self.headers.get('Transfer-Encoding'):
            raise ValueError('Invalid request encoding')

        lengths = self.headers.get_all('Content-Length') or []

        if len(lengths) != 1:
            raise ValueError('Invalid request length')

        length = int(lengths[0])

        if not 1 <= length <= 4096:
            raise ValueError('Invalid identity request size')

        raw = self.rfile.read(length)

        if len(raw) != length:
            raise ValueError('Incomplete request')

        data = json.loads(raw.decode('utf-8'))

        if not isinstance(data, dict):
            raise ValueError('Invalid identity request')

        return data
    def read_json(self):
        n = int(self.headers.get('Content-Length', '0'))
        if n <= 0 or n > 10000:
            raise ValueError('Invalid request size')
        return json.loads(self.rfile.read(n))
    def do_HEAD(self):
        self.send_response(405)
        self.send_header('Allow', 'GET, POST')
        self.send_header('Content-Length', '0')
        self.end_headers()
    def do_GET(self):
        u = urlparse(self.path)

        if u.path.startswith('/api/identity/'):
            if not IDENTITY_ENABLED:
                return self.reply(404, {
                    'error': 'Not found'
                })

            if u.path != '/api/identity/me':
                return self.reply(404, {
                    'error': 'Unknown identity action'
                })

            if not identity_rate_allowed(
                'identity:read:global', 300, 60
            ):
                return self.reply(429, {
                    'error': 'Too many requests'
                })

            if self.identity_session_token() is None:
                return self.reply(401, {
                    'error': 'Not signed in'
                })

            try:
                with connect() as db:
                    identity = self.current_identity(db)

                if identity is None:
                    return self.reply(
                        401,
                        {'error': 'Not signed in'},
                        cookie=clear_identity_cookie()
                    )

                return self.reply(200, {
                    'identity': public_identity(identity),
                    'metallicUnlocked': identity['metallic_unlocked'],
                    'discoverable': identity['discoverable'],
                    'allowRequests': identity['allow_requests']
                })

            except Exception:
                logging.error('Identity lookup failed')

                return self.reply(500, {
                    'error': 'Server error'
                })
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
            if u.path.startswith('/api/identity/'):
                if not IDENTITY_ENABLED:
                    return self.reply(404, {
                        'error': 'Not found'
                    })

                if not self.valid_identity_request():
                    return self.reply(403, {
                        'error': 'Invalid request origin or type'
                    })

                if not identity_rate_allowed(
                    'identity:global', 120, 60
                ):
                    return self.reply(429, {
                        'error': 'Too many requests. Try again shortly.'
                    })

                if u.path == '/api/identity/register':
                    if not IDENTITY_REGISTRATION_ENABLED:
                        return self.reply(403, {
                            'error': 'Registration is currently unavailable'
                        })

                    data = self.read_identity_json()

                    username = normalize_username(
                        data.get('username')
                    )

                    if not identity_action_allowed(
                        'register', username
                    ):
                        return self.reply(429, {
                            'error': 'Please try again later'
                        })

                    result = register_identity(
                        username,
                        data.get('displayName'),
                        data.get('password')
                    )

                    if result is None:
                        return self.reply(409, {
                            'error': 'Username unavailable'
                        })
                    rotate_previous_identity_session(
                        self.identity_session_token(),
                        result['sessionToken']
                    )
                    return self.reply(
                        201,
                        {
                            'identity': result['identity'],
                            'recoveryKey': result['recoveryKey']
                        },
                        cookie=make_identity_cookie(
                            result['sessionToken']
                        )
                    )

                if u.path == '/api/identity/login':
                    data = self.read_identity_json()

                    username = normalize_username(
                        data.get('username')
                    )

                    if not identity_action_allowed(
                        'login', username
                    ):
                        return self.reply(429, {
                            'error': 'Please try again later'
                        })

                    result = authenticate_identity(
                        username,
                        data.get('password')
                    )

                    if result is None:
                        return self.reply(401, {
                            'error': 'Invalid username or password'
                        })
                    rotate_previous_identity_session(
                        self.identity_session_token(),
                        result['sessionToken']
                    )
                    return self.reply(
                        200,
                        {
                            'identity': result['identity']
                        },
                        cookie=make_identity_cookie(
                            result['sessionToken']
                        )
                    )
                if u.path == '/api/identity/recover':
                    data = self.read_identity_json()

                    username = normalize_username(
                        data.get('username')
                    )

                    if not identity_action_allowed(
                        'recover', username
                    ):
                        return self.reply(429, {
                            'error': 'Please try again later'
                        })

                    result = recover_identity(
                        username,
                        data.get('recoveryKey'),
                        data.get('newPassword')
                    )

                    if result is None:
                        return self.reply(401, {
                            'error': 'Invalid recovery credentials'
                        })
                    rotate_previous_identity_session(
                        self.identity_session_token(),
                        result['sessionToken']
                    )
                    return self.reply(
                        200,
                        {
                            'identity': result['identity'],
                            'recoveryKey': result['recoveryKey']
                        },
                        cookie=make_identity_cookie(
                            result['sessionToken']
                        )
                    )

                if u.path == '/api/identity/profile':
                    raw_token = self.identity_session_token()

                    if raw_token is None:
                        return self.reply(401, {
                            'error': 'Not signed in'
                        })

                    if not identity_rate_allowed(
                        'identity:profile:'
                        + identity_secret_hash(raw_token),
                        20, 60
                    ):
                        return self.reply(429, {
                            'error': 'Please try again later'
                        })

                    data = self.read_identity_json()

                    try:
                        result = update_identity_profile(
                            raw_token, data
                        )

                    except PermissionError:
                        return self.reply(403, {
                            'error': 'Metallic collection is locked'
                        })

                    if result is None:
                        return self.reply(
                            401,
                            {'error': 'Not signed in'},
                            cookie=clear_identity_cookie()
                        )

                    return self.reply(200, result)

                if u.path == '/api/identity/delete':
                    raw_token = self.identity_session_token()

                    if raw_token is None:
                        return self.reply(401, {
                            'error': 'Not signed in'
                        })

                    if not identity_rate_allowed(
                        'identity:delete:'
                        + identity_secret_hash(raw_token),
                        5, 900
                    ):
                        return self.reply(429, {
                            'error': 'Please try again later'
                        })

                    data = self.read_identity_json()

                    if data.get('confirm') is not True:
                        return self.reply(400, {
                            'error': 'Deletion confirmation required'
                        })

                    result = delete_identity(
                        raw_token,
                        data.get('password')
                    )

                    if result == 'unauthorized':
                        return self.reply(
                            401,
                            {'error': 'Not signed in'},
                            cookie=clear_identity_cookie()
                        )
                    if result == 'rate_limited':
                        return self.reply(429, {
                            'error': 'Please try again later'
                        })
                    if result == 'invalid_password':
                        return self.reply(403, {
                            'error': 'Incorrect password'
                        })

                    return self.reply(
                        200,
                        {'ok': True},
                        cookie=clear_identity_cookie()
                    )
                if u.path == '/api/identity/logout':
                    self.read_identity_json()

                    raw_token = self.identity_session_token()

                    if raw_token is not None:
                        with connect() as db:
                            revoke_identity_session(
                                db, raw_token
                            )

                    return self.reply(
                        200,
                        {'ok': True},
                        cookie=clear_identity_cookie()
                    )

                return self.reply(404, {
                    'error': 'Unknown identity action'
                })

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
            if u.path == '/api/action':
                n = int(
                    self.headers.get('Content-Length', '0')
                )

                if n <= 0 or n > 410_000:
                    return self.reply(413, {
                        'error': 'Request too large'
                    })

                data = json.loads(self.rfile.read(n))

                if not isinstance(data, dict):
                    return self.reply(400, {
                        'error': 'Invalid request'
                    })

                if n > 10000 and not (
                    data.get('action') == 'add'
                    and isinstance(data.get('image'), str)
                ):
                    return self.reply(413, {
                        'error': 'Request too large'
                    })
            else:
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
                    and valid_push_endpoint(sub.get('endpoint'))
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
                added_photo = None
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
                        name = str(
                            data.get('name', '')
                        ).strip()[:100]

                        qty = str(
                            data.get('qty', '')
                        ).strip()[:20]

                        if not name or len(items) >= 300:
                            return self.reply(400, {
                                'error': 'Item missing or list full'
                            })

                        encoded = data.get('image')

                        if encoded is not None:
                            photo_count = sum(
                                bool(x.get('photo'))
                                for x in items
                            )

                            if photo_count >= 50:
                                return self.reply(400, {
                                    'error': 'List photo limit reached'
                                })

                            content = prepare_photo(encoded)

                            added_photo = upload_photo(
                                lid, content
                            )

                        new_item = {
                            'id': token(),
                            'name': name,
                            'qty': qty,
                            'done': False,
                            'addedDuring': shop['id'] if shop else None
                        }

                        if added_photo:
                            new_item['photo'] = added_photo

                        items.insert(0, new_item)

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

                    try:
                        db.execute(
                            'UPDATE lists SET data=%s, updated=%s WHERE id=%s',
                            (json.dumps(obj), now, lid)
                        )

                        if added_photo:
                            db.commit()

                    except Exception:
                        if added_photo:
                            delete_photo(lid, added_photo)

                        raise
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
        except IdentityBusyError:
            return self.reply(503, {
                'error': 'Server busy. Please try again shortly.'
            })
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
    ProtectedHTTPServer(('0.0.0.0', port), Handler).serve_forever()
