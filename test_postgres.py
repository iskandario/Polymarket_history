"""Optional real PostgreSQL integration test. All test data is rolled back."""
import os
import uuid
import pytest
import psycopg
from psycopg.types.json import Jsonb
from eth_abi import decode, encode
from wallet import ROOT, ZERO, DEFAULT_WALLET, reconcile, signature, scan, matches, SINGLE


@pytest.mark.skipif(not os.getenv('TEST_DATABASE_URL'),reason='Set TEST_DATABASE_URL for PostgreSQL integration test')
def test_sql_uint256_deduplication_and_batch_reconciliation():
    ctf = '0x'+'11'*20
    assets = {'erc20':{},'erc1155':{ctf:'test'}}
    run = 'test-'+uuid.uuid4().hex
    huge = 2**256-1
    class Chain:
        def block(self,n): return {'hash':'0xblock'}
        def call(self,method,params):
            if method == 'eth_getCode': return '0x01'
            assert method == 'eth_call'
            assert params[1] == '0x1'
            data = params[0]['data']
            assert data[:10] == signature('balanceOfBatch(address[],uint256[])')[:10]
            owners,ids = decode(['address[]','uint256[]'],bytes.fromhex(data[10:]))
            assert all(owner == DEFAULT_WALLET for owner in owners)
            return '0x'+encode(['uint256[]'],[[huge if token == huge else 0 for token in ids]]).hex()
    with psycopg.connect(os.environ['TEST_DATABASE_URL']) as db:
        try:
            db.execute((ROOT/'schema.sql').read_text())
            db.execute('INSERT INTO runs(id,config,target_block,target_hash,target_timestamp) VALUES (%s,%s,1,%s,1)',(run,Jsonb({}),'0xblock'))
            db.execute('INSERT INTO raw_logs VALUES (%s,1,%s,%s,0,0,%s,%s)',(run,'0xblock','0xtx',ctf,Jsonb({})))
            for _ in range(2):
                for idx,token,amount in [(0,huge,huge),(1,7,0)]:
                    db.execute('INSERT INTO movements VALUES (%s,%s,0,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                               (run,'0xtx',idx,ctf,token,ZERO,DEFAULT_WALLET,amount,amount))
            assert db.execute('SELECT count(*) FROM movements WHERE run_id=%s',(run,)).fetchone()[0] == 2
            ok,negative,rows,proofs = reconcile(db,Chain(),run,DEFAULT_WALLET,assets,1,'0xblock')
            assert ok and negative == 0
            assert len(rows) == 2 and len(proofs) == 1
            assert {r['onchain_raw'] for r in rows} == {'0',str(huge)}
            db.execute('UPDATE movements SET delta=1 WHERE run_id=%s AND token_id=7',(run,))
            ok,_,rows,_ = reconcile(db,Chain(),run,DEFAULT_WALLET,assets,1,'0xblock')
            assert not ok
            assert any(r['difference_raw'] == '1' for r in rows)
        finally:
            db.rollback()


@pytest.mark.skipif(not os.getenv('TEST_DATABASE_URL'),reason='Set TEST_DATABASE_URL for PostgreSQL integration test')
def test_copy_scanner_prefetch_and_resume():
    from test_wallet import ASSETS, log
    run = 'test-'+uuid.uuid4().hex
    event = log(SINGLE,ZERO,DEFAULT_WALLET,[42],[123])
    event.update(blockNumber='0x1',blockHash='block1',transactionHash='tx',transactionIndex='0x0',logIndex='0x0',removed=False)
    class Chain:
        def block(self,n): return {'hash':f'block{n}'}
        def logs(self,query,lo,hi,cap):
            return [event] if lo <= 1 <= hi and matches(event,query) else []
    with psycopg.connect(os.environ['TEST_DATABASE_URL']) as db:
        try:
            db.execute((ROOT/'schema.sql').read_text())
            db.execute('INSERT INTO runs(id,config,target_block,target_hash,target_timestamp) VALUES (%s,%s,2,%s,1)',(run,Jsonb({}),'block2'))
            for _ in range(2):
                scan(db,Chain(),run,DEFAULT_WALLET,ASSETS,2,'block2',1,1000,2)
            assert db.execute('SELECT next_block FROM runs WHERE id=%s',(run,)).fetchone()[0] == 3
            assert db.execute('SELECT count(*),sum(delta) FROM movements WHERE run_id=%s',(run,)).fetchone() == (1,123)
        finally:
            db.rollback()
