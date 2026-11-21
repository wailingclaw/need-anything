"""Need Anything? — minimal, dependency-free collaborative shopping server."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import json, sqlite3, secrets, time, os, threading, logging
from concurrent.futures import ThreadPoolExecutor
PUSH_POOL=ThreadPoolExecutor(max_workers=3)

ROOT=Path(__file__).resolve().parent
DB=Path(os.environ.get('SHOPPING_DB',str(ROOT/'shopping.sqlite3')))
LOCK=threading.RLock()

def connect():
    db=sqlite3.connect(DB,timeout=15)
    db.row_factory=sqlite3.Row
    return db

def init():
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS lists (id TEXT PRIMARY KEY, secret TEXT NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS subscriptions (list_id TEXT NOT NULL, endpoint TEXT NOT NULL, subscription TEXT NOT NULL, PRIMARY KEY(list_id, endpoint))')

def token(): return secrets.token_urlsafe(24)

def send_push(list_id, initiator_endpoint, message, url):
    private=os.environ.get('VAPID_PRIVATE_KEY','')
    email=os.environ.get('VAPID_CONTACT_EMAIL','')
    if not private or not email:return
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logging.warning('Install requirements.txt to enable push')
        return
    with connect() as db:
        rows=db.execute('SELECT endpoint,subscription FROM subscriptions WHERE list_id=?',(list_id,)).fetchall()
    for row in rows:
        if row['endpoint']==initiator_endpoint:continue
        try:
            webpush(subscription_info=json.loads(row['subscription']),data=json.dumps({'title':'Need Anything?','body':message,'url':url}),vapid_private_key=private,vapid_claims={'sub':'mailto:'+email},ttl=300)
        except WebPushException as exc:
            if getattr(getattr(exc,'response',None),'status_code',None) in (404,410):
                with connect() as db:db.execute('DELETE FROM subscriptions WHERE list_id=? AND endpoint=?',(list_id,row['endpoint']))
            else:logging.warning('Push delivery failed: %s',type(exc).__name__)
        except Exception:logging.exception('Push delivery error')

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*a,**kw): super().__init__(*a,directory=str(ROOT),**kw)
    def log_message(self,format,*args):
        pass  # Avoid writing invitation tokens into access logs.
    def reply(self,status,data):
        body=json.dumps(data).encode()
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    
    def read_json(self):
        n = int(self.headers.get('Content-Length', '0'))
        if n <= 0 or n > 10000:
            raise ValueError('Invalid request size')
        return json.loads(self.rfile.read(n))

    def do_GET(self):
        u=urlparse(self.path)
        if u.path=='/api/list':
            q=parse_qs(u.query);lid=q.get('id',[''])[0];key=q.get('key',[''])[0]
            with LOCK,connect() as db:
                row=db.execute('SELECT secret,data,updated FROM lists WHERE id=?',(lid,)).fetchone()
            if not row or not secrets.compare_digest(row['secret'],key):return self.reply(404,{'error':'List not found or invitation invalid'})
            return self.reply(200,{'list':json.loads(row['data']),'version':row['updated']})
        if u.path=='/api/health':return self.reply(200,{'ok':True})
        if u.path=='/api/push-config':return self.reply(200,{'publicKey':os.environ.get('VAPID_PUBLIC_KEY','')})
        
        public_files = {
            '/', '/index.html', '/sw.js',
            '/manifest.webmanifest', '/icon.svg'
        }
        if u.path not in public_files:
            return self.reply(404, {'error': 'Not found'})
        self.path = '/index.html' if u.path == '/' else u.path
        return super().do_GET()

    def do_POST(self):
        u=urlparse(self.path)
        try:
            data=self.read_json()
            if u.path=='/api/create':
                name=str(data.get('name','')).strip()[:60]
                if not name: return self.reply(400,{'error':'List name required'})
                lid=token();key=token();obj={'id':lid,'name':name,'items':[],'shopping':None,'events':[]}
                with LOCK,connect() as db:db.execute('INSERT INTO lists VALUES (?,?,?,?)',(lid,key,json.dumps(obj),time.time()))
                return self.reply(200,{'id':lid,'key':key,'list':obj})
            if u.path=='/api/subscribe':
                lid=str(data.get('id',''));key=str(data.get('key',''));sub=data.get('subscription')
                if not isinstance(sub,dict) or not isinstance(sub.get('endpoint'),str) or not sub['endpoint'].startswith('https://') or len(json.dumps(sub))>8000 or not isinstance(sub.get('keys'),dict) or not all(isinstance(sub['keys'].get(k),str) for k in ('auth','p256dh')):return self.reply(400,{'error':'Invalid subscription'})
                with LOCK,connect() as db:
                    row=db.execute('SELECT secret FROM lists WHERE id=?',(lid,)).fetchone()
                    if not row or not secrets.compare_digest(row['secret'],key):return self.reply(404,{'error':'Invalid invitation'})
                    db.execute('INSERT OR REPLACE INTO subscriptions VALUES (?,?,?)',(lid,sub['endpoint'],json.dumps(sub)))
                return self.reply(200,{'ok':True})
            if u.path=='/api/unsubscribe':
                lid=str(data.get('id',''));key=str(data.get('key',''));endpoint=str(data.get('endpoint',''))
                with LOCK,connect() as db:
                    row=db.execute('SELECT secret FROM lists WHERE id=?',(lid,)).fetchone()
                    if not row or not secrets.compare_digest(row['secret'],key):return self.reply(404,{'error':'Invalid invitation'})
                    db.execute('DELETE FROM subscriptions WHERE list_id=? AND endpoint=?',(lid,endpoint))
                return self.reply(200,{'ok':True})
            if u.path=='/api/action':
                lid=str(data.get('id',''));key=str(data.get('key',''));action=data.get('action');person=str(data.get('person','Someone')).strip()[:40] or 'Someone'
                with LOCK,connect() as db:
                    row=db.execute('SELECT secret,data FROM lists WHERE id=?',(lid,)).fetchone()
                    if not row or not secrets.compare_digest(row['secret'],key):return self.reply(404,{'error':'Invalid invitation'})
                    obj=json.loads(row['data']);items=obj['items'];shop=obj['shopping'];now=time.time()
                    if action=='add':
                        name=str(data.get('name','')).strip()[:100];qty=str(data.get('qty','')).strip()[:20]
                        if not name or len(items)>=300:return self.reply(400,{'error':'Item missing or list full'})
                        items.append({'id':token(),'name':name,'qty':qty,'done':False,'addedDuring':shop['id'] if shop else None})
                    elif action=='check':
                        item=next((x for x in items if x['id']==data.get('itemId')),None)
                        if not item:return self.reply(404,{'error':'Item not found'})
                        item['done']=bool(data.get('done'))
                    elif action=='remove':obj['items']=[x for x in items if x['id']!=data.get('itemId')]
                    elif action=='start':
                        if shop:return self.reply(409,{'error':'Someone is already shopping'})
                        obj['shopping']={'id':token(),'by':person,'lastCall':False}
                        obj['events'].append({'id':token(),'type':'start','by':person,'at':now})
                    elif action=='lastCall':
                        if not shop:
                            return self.reply(409, {'error':'No active shopping trip'})
                        if shop['lastCall']:
                            return self.reply(409, {'error':'Last Call already sent'})
                        if not shop['lastCall']:

                            shop['lastCall']=True;obj['events'].append({'id':token(),'type':'lastCall','by':person,'at':now})
                    elif action=='done':
                        if not shop:return self.reply(409,{'error':'No active shopping trip'})
                        obj['shopping']=None;obj['events'].append({'id':token(),'type':'done','by':person,'at':now})
                    elif action=='rename':
                        name=str(data.get('name','')).strip()[:60]
                        if not name:return self.reply(400,{'error':'Name required'})
                        obj['name']=name
                    else:return self.reply(400,{'error':'Unknown action'})
                    obj['events']=obj['events'][-30:]
                    db.execute('UPDATE lists SET data=?,updated=? WHERE id=?',(json.dumps(obj),now,lid))
                if action in ('start','lastCall'):
                    message=f'{person} is shopping! Need anything?' if action=='start' else f'{person} is almost finished! Anything else?'
                    # Only queue once for a Last Call event.
                    if action=='start' or (action=='lastCall' and shop and shop['lastCall'] and obj['events'][-1]['type']=='lastCall' and obj['events'][-1]['at']==now):
                        PUSH_POOL.submit(send_push,lid,str(data.get('senderEndpoint','')),message,'/?list='+lid+'&key='+key)
                return self.reply(200,{'list':obj,'version':now})
            return self.reply(404,{'error':'Not found'})
        except (ValueError,TypeError,json.JSONDecodeError):return self.reply(400,{'error':'Invalid request'})
        except Exception:return self.reply(500,{'error':'Server error'})

if __name__=='__main__':
    init();port=int(os.environ.get('PORT','8000'))
    print(f'Need Anything? running at http://localhost:{port}',flush=True)
    ThreadingHTTPServer(('0.0.0.0',port),Handler).serve_forever()
