"""Integration tests against a disposable workspace on a running deployment.
Run: SLOTSYNC_URL=https://your-demo.vercel.app python -m pytest -q
"""
import os, uuid
from datetime import date
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
URL=os.environ.get('SLOTSYNC_URL','http://127.0.0.1:8000').rstrip('/')
def call(path,body=None):
    return httpx.post(URL+path,json=body,timeout=45) if body is not None else httpx.get(URL+path,timeout=45)
@pytest.fixture
def workspace():
    r=call('/api/rooms',{});assert r.status_code==200
    return r.json()['room']
def body(room,**kw):
    return dict(room=room,key=str(uuid.uuid4()),resource='GPU workstation',day=str(date.today()),hour=9,client='Test A',clock=0,**kw)
def test_health():
    r=call('/api/health');assert r.status_code==200 and r.json()['storage']=='PostgreSQL'
def test_concurrent_one_winner(workspace):
    a=body(workspace);b={**a,'key':str(uuid.uuid4()),'client':'Test B'}
    with ThreadPoolExecutor(2) as pool: rr=list(pool.map(lambda x:call('/api/book',x),[a,b]))
    assert sorted(r.status_code for r in rr)==[201,409]
    s=call(f'/api/state?room={workspace}&day={date.today()}').json()
    assert len(s['bookings'])==1 and s['clock']==2

def test_retry_same_result(workspace):
    b=body(workspace);r=call('/api/book',b);again=call('/api/book',b)
    assert r.status_code==again.status_code==201
    assert again.json()['replayed'] is True
    assert again.json()['booking_id']==r.json()['booking_id']
    s=call(f'/api/state?room={workspace}&day={date.today()}').json();assert len(s['bookings'])==1 and len(s['events'])==1

def test_key_mismatch(workspace):
    b=body(workspace);assert call('/api/book',b).status_code==201
    r=call('/api/book',{**b,'hour':10});assert r.status_code==409 and 'different data' in r.text

def test_logical_clock(workspace):
    b=body(workspace);b['clock']=7
    assert call('/api/book',b).json()['clock']==8

def test_invalid_resource(workspace):
    b=body(workspace);b['resource']='Unknown';assert call('/api/book',b).status_code==422

def test_invalid_hour(workspace):
    b=body(workspace);b['hour']=18;assert call('/api/book',b).status_code==422

def test_workspace_isolation(workspace):
    second=call('/api/rooms',{}).json()['room']
    assert call('/api/book',body(workspace)).status_code==201
    assert call('/api/book',body(second)).status_code==201

def test_invalid_room():
    assert call('/api/state?room=bad&day='+str(date.today())).status_code==422

def test_missing_room():
    assert call('/api/state?room='+str(uuid.uuid4())+'&day='+str(date.today())).status_code==404
