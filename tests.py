import os, tempfile
os.environ['DB_FILE']=os.path.join(tempfile.gettempdir(),'qt_test.db')
os.environ['REQUIRE_LICENSE']='0'
os.environ['GEMINI_API_KEY']=''
os.environ['RAPIDAPI_KEY']=''
from main import poisson_matrix, true_ev, fractional_kelly, hash_key, make_key, validate_future_kickoff, Ineligible
from datetime import datetime, timedelta, timezone

def test_math():
    m=poisson_matrix(1.6,1.1); assert abs(sum(map(sum,m))-1)<1e-9

def test_ev():
    assert abs(true_ev(55,2.0)-10.0)<1e-9
    p,s=fractional_kelly(55,2.0,1000); assert p>0 and s>0

def test_key():
    k=make_key(); assert k.startswith('QT-'); assert hash_key(k)!=k

def test_future():
    future=datetime.now(timezone.utc)+timedelta(hours=2)
    assert validate_future_kickoff(future.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Ho_Chi_Minh')).date().isoformat(), future.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Ho_Chi_Minh')).strftime('%H:%M'))>datetime.now(timezone.utc)
    past=datetime.now(timezone.utc)-timedelta(hours=1)
    try: validate_future_kickoff(past.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Ho_Chi_Minh')).date().isoformat(), past.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Ho_Chi_Minh')).strftime('%H:%M')); raise AssertionError
    except Ineligible: pass

if __name__=='__main__':
    for n,v in list(globals().items()):
        if n.startswith('test_'): v(); print('PASS',n)
    print('ALL TESTS PASSED')

def test_key_isolation_and_limit():
    import main
    p=main.db_placeholder()
    k1=main.make_key(); k2=main.make_key()
    with main.db() as conn:
        conn.execute(f"INSERT INTO access_keys(key_hash,note,status,created_at,max_uses,used_count) VALUES ({p},{p},'ACTIVE',{p},2,0)",(main.hash_key(k1),'u1',main.iso_now()))
        conn.execute(f"INSERT INTO access_keys(key_hash,note,status,created_at,max_uses,used_count) VALUES ({p},{p},'ACTIVE',{p},1,0)",(main.hash_key(k2),'u2',main.iso_now()))
        conn.commit()
    u1=main.authenticate(k1,consume=True); u2=main.authenticate(k2,consume=True)
    assert u1['user_id'] != u2['user_id']
    try:
        main.authenticate(k2,consume=True); raise AssertionError('limit should block')
    except main.AuthError: pass
