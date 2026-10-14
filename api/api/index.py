"""SlotSync: transaction-backed mutual exclusion for fixed-hour reservations."""
import os, json, hashlib, uuid
from datetime import date, timedelta
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
app = FastAPI(title="SlotSync RPC", version="1.0.0")
SCHEMA = """
CREATE TABLE IF NOT EXISTS ss_rooms (id text PRIMARY KEY, clock bigint NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ss_bookings (id text PRIMARY KEY, room text NOT NULL REFERENCES ss_rooms(id), resource text NOT NULL, day text NOT NULL, hour int NOT NULL, client text NOT NULL, clock bigint NOT NULL, UNIQUE(room,resource,day,hour));
CREATE TABLE IF NOT EXISTS ss_requests (room text NOT NULL, key text NOT NULL, fingerprint text NOT NULL, status int NOT NULL, body jsonb NOT NULL, PRIMARY KEY(room,key));
CREATE TABLE IF NOT EXISTS ss_events (id bigserial PRIMARY KEY, room text NOT NULL, clock bigint NOT NULL, kind text NOT NULL, client text NOT NULL, detail text NOT NULL);
"""
RESOURCES = ["GPU workstation", "Robotics bench", "Meeting room"]
@contextmanager
def db():
    url = os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_URL')
    if not url: raise HTTPException(503, 'Database is not configured. No booking was saved.')
    try:
        with psycopg.connect(url, connect_timeout=10, row_factory=dict_row) as conn:
            conn.execute(SCHEMA)
            yield conn
    except psycopg.Error:
        raise HTTPException(503, 'Database temporarily unavailable. Retry with the same request key.')

def room_valid(room):
    try: uuid.UUID(room)
    except (ValueError, AttributeError): raise HTTPException(422, 'Invalid room link')

class Booking(BaseModel):
    room: str
    key: str = Field(min_length=8,max_length=100)
    resource: str
    day: date
    hour: int = Field(ge=9,le=16)
    client: str = Field(min_length=1,max_length=32,pattern=r'^[A-Za-z0-9 _-]+$')
    clock: int = Field(default=0,ge=0,le=1000000000)

@app.get('/api/health')
def health():
    with db() as c: c.execute('SELECT 1')
    return {'ok': True, 'storage':'PostgreSQL', 'coordination':'transactional centralized mutex'}

@app.post('/api/rooms')
def create_room():
    room=str(uuid.uuid4())
    with db() as c: c.execute('INSERT INTO ss_rooms(id) VALUES(%s)',(room,))
    return {'room':room}

@app.get('/api/state')
def state(room: str, day: date):
    room_valid(room)
    with db() as c:
        r=c.execute('SELECT clock FROM ss_rooms WHERE id=%s',(room,)).fetchone()
        if not r: raise HTTPException(404,'Workspace not found')
        b=c.execute('SELECT * FROM ss_bookings WHERE room=%s AND day=%s ORDER BY hour,resource',(room,str(day))).fetchall()
        e=c.execute('SELECT clock,kind,client,detail FROM ss_events WHERE room=%s ORDER BY id DESC LIMIT 30',(room,)).fetchall()
    return {'clock':r['clock'],'bookings':b,'events':e,'resources':RESOURCES}

@app.post('/api/book')
def book(b: Booking):
    room_valid(b.room)
    if b.resource not in RESOURCES: raise HTTPException(422,'Unknown resource')
    if not date.today() <= b.day <= date.today()+timedelta(days=30): raise HTTPException(422,'Choose today through the next 30 days')
    # Clock is excluded: a retry may carry a newer observed clock but represents the same operation.
    payload = [b.resource,str(b.day),b.hour,b.client]
    fp=hashlib.sha256(json.dumps(payload).encode()).hexdigest()
    with db() as c:
        # All cooperating workers lock the same durable room row, not process memory.
        r=c.execute('SELECT clock FROM ss_rooms WHERE id=%s FOR UPDATE',(b.room,)).fetchone()
        if not r: raise HTTPException(404,'Workspace not found')
        old=c.execute('SELECT * FROM ss_requests WHERE room=%s AND key=%s',(b.room,b.key)).fetchone()
        if old:
            if old['fingerprint']!=fp: raise HTTPException(409,'Request key already used for different data')
            return JSONResponse({**old['body'],'replayed':True},status_code=old['status'])
        count=c.execute('SELECT count(*) AS n FROM ss_events WHERE room=%s',(b.room,)).fetchone()['n']
        if count>=500: raise HTTPException(429,'Workspace demo limit reached. Create a new workspace.')
        clock=max(r['clock'],b.clock)+1
        c.execute('UPDATE ss_rooms SET clock=%s WHERE id=%s',(clock,b.room))
        ident=str(uuid.uuid4())
        win=c.execute('INSERT INTO ss_bookings(id,room,resource,day,hour,client,clock) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(room,resource,day,hour) DO NOTHING RETURNING id',(ident,b.room,b.resource,str(b.day),b.hour,b.client,clock)).fetchone()
        status=201 if win else 409
        body={'ok':bool(win),'clock':clock,'message':'Booking confirmed' if win else 'Slot already taken. No second booking was saved.','booking_id':ident if win else None,'replayed':False}
        c.execute('INSERT INTO ss_requests VALUES(%s,%s,%s,%s,%s::jsonb)',(b.room,b.key,fp,status,json.dumps(body)))
        c.execute('INSERT INTO ss_events(room,clock,kind,client,detail) VALUES(%s,%s,%s,%s,%s)',(b.room,clock,'confirmed' if win else 'conflict',b.client,f'{b.resource} | {b.day} | {b.hour}:00'))
    return JSONResponse(body,status_code=status)
