"""Reconstruct a single Polygon address using only standard JSON-RPC and SQL."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from concurrent.futures import ThreadPoolExecutor
from collections import deque

from eth_abi import decode, encode
from eth_utils import keccak
import httpx
import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parent
DEFAULT_WALLET = '0x46b353667fd7d846af3bbeda6584b0e5b883d3de'
ZERO = '0x' + '0' * 40


def signature(text):
    return '0x' + keccak(text=text).hex()


TRANSFER = signature('Transfer(address,address,uint256)')
SINGLE = signature('TransferSingle(address,address,address,uint256,uint256)')
BATCH = signature('TransferBatch(address,address,address,uint256[],uint256[])')


def address(value):
    if not re.fullmatch(r'0x[0-9a-fA-F]{40}', value):
        raise ValueError('Expected a 20-byte 0x address')
    return value.lower()


def topic_address(value):
    return '0x' + address(value)[2:].zfill(64)


class RPCError(RuntimeError):
    pass


class RangeLimit(RPCError):
    pass


class RPC:
    def __init__(self, url):
        self.url = url
        self.client = httpx.Client(timeout=40)
        logging.getLogger('httpx').setLevel(logging.WARNING)

    def call(self, method, params):
        # Never log the URL: providers often put an API key in the path/query.
        for attempt in range(6):
            try:
                r = self.client.post(self.url, json={
                    'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
                if r.status_code in (413, 504) and method == 'eth_getLogs':
                    raise RangeLimit(f'{method}: HTTP {r.status_code}')
                if r.status_code in (429, 500, 502, 503, 504):
                    raise httpx.TransportError('temporary HTTP failure')
                if r.status_code != 200:
                    raise RPCError(f'{method}: HTTP {r.status_code}')
                data = r.json()
                if not isinstance(data, dict) or data.get('id') != 1:
                    raise RPCError(f'{method}: invalid JSON-RPC envelope')
                if 'error' in data:
                    err = data['error']
                    msg = (str(err.get('message', '')) + ' ' + str(err.get('data', ''))).lower()
                    if method == 'eth_getLogs' and any(s in msg for s in (
                        'block range', 'too many results', 'response size',
                        'query returned more', 'limit exceeded', 'range too',
                        'maximum block', 'query timeout', 'timed out')):
                        raise RangeLimit(f'{method}: range/result limit')
                    if any(s in msg for s in ('rate limit', 'too many requests')):
                        raise httpx.TransportError('RPC rate limit')
                    raise RPCError(f'{method}: RPC error {err.get("code")}')
                if 'result' not in data:
                    raise RPCError(f'{method}: missing result')
                return data['result']
            except (httpx.TransportError, ValueError):
                if attempt == 5:
                    raise RPCError(f'{method}: transport/JSON failed after retries') from None
                time.sleep(min(2 ** attempt, 16))
        raise AssertionError('unreachable')

    def block(self, number):
        result = self.call('eth_getBlockByNumber', [hex(number) if isinstance(number, int) else number, False])
        if not result or not result.get('hash'):
            raise RPCError('Requested block is unavailable')
        return result

    def logs(self, query, lo, hi, cap):
        try:
            logs = self.call('eth_getLogs', [{**query, 'fromBlock': hex(lo), 'toBlock': hex(hi)}])
            if not isinstance(logs, list):
                raise RPCError('eth_getLogs did not return an array')
            if len(logs) >= cap:
                raise RangeLimit('Possible result truncation')
            return logs
        except RangeLimit:
            if lo == hi:
                # A single busy block cannot be split further: read its receipts.
                return self.block_logs(query, lo)
            mid = (lo + hi) // 2
            return self.logs(query, lo, mid, cap) + self.logs(query, mid + 1, hi, cap)

    def block_logs(self, query, number):
        header = self.block(number)
        found = []
        for tx in header['transactions']:
            receipt = self.call('eth_getTransactionReceipt', [tx])
            if not receipt or receipt['blockHash'] != header['hash']:
                raise RPCError('Receipt unavailable or reorg during block fallback')
            found.extend(log for log in receipt['logs'] if matches(log, query))
        return found


def matches(log, query):
    if log['address'].lower() not in query['address']:
        return False
    for index, wanted in enumerate(query['topics']):
        if wanted is None:
            continue
        choices = wanted if isinstance(wanted, list) else [wanted]
        if index >= len(log['topics']) or log['topics'][index].lower() not in choices:
            return False
    return True


def filters(wallet, assets):
    who = topic_address(wallet)
    queries = []
    if assets['erc20']:
        for topics in ([TRANSFER, who], [TRANSFER, None, who]):
            queries.append({'address': list(assets['erc20']), 'topics': topics})
    if assets['erc1155']:
        for topics in ([[SINGLE, BATCH], None, who], [[SINGLE, BATCH], None, None, who]):
            queries.append({'address': list(assets['erc1155']), 'topics': topics})
    return queries


def movements(log, wallet, assets):
    contract = log['address'].lower()
    topics = [t.lower() for t in log['topics']]
    raw = bytes.fromhex(log['data'][2:])
    if contract in assets['erc20'] and topics[0] == TRANSFER:
        if len(topics) != 3 or len(raw) != 32:
            raise ValueError('Malformed ERC20 Transfer')
        sender, recipient = ('0x' + t[-40:] for t in topics[1:3])
        items = [(0, int.from_bytes(raw, 'big'))]
    elif contract in assets['erc1155'] and topics[0] in (SINGLE, BATCH):
        if len(topics) != 4:
            raise ValueError('Malformed ERC1155 topics')
        sender, recipient = ('0x' + t[-40:] for t in topics[2:4])
        if topics[0] == SINGLE:
            if len(raw) != 64:
                raise ValueError('Malformed TransferSingle')
            items = [decode(['uint256', 'uint256'], raw)]
        else:
            ids, amounts = decode(['uint256[]', 'uint256[]'], raw)
            if len(ids) != len(amounts):
                raise ValueError('TransferBatch array lengths differ')
            items = list(zip(ids, amounts))
    else:
        raise ValueError('Unexpected contract/event')
    if wallet not in (sender, recipient):
        raise ValueError('RPC returned unrelated transfer')
    # Two independent comparisons: self-transfer must have delta zero.
    direction = int(recipient == wallet) - int(sender == wallet)
    return [(i, contract, token, sender, recipient, amount, direction * amount)
            for i, (token, amount) in enumerate(items)]


def check_hash(rpc, number, expected):
    if rpc.block(number)['hash'] != expected:
        raise RPCError('Block hash changed. Start a new run; this run is not verified.')


def scan(db, rpc, run_id, wallet, assets, target, target_hash, chunk, cap, workers):
    queries = filters(wallet, assets)
    db.execute('CREATE TEMP TABLE IF NOT EXISTS incoming_logs (LIKE raw_logs) ON COMMIT PRESERVE ROWS')
    db.execute('CREATE TEMP TABLE IF NOT EXISTS incoming_movements (LIKE movements) ON COMMIT PRESERVE ROWS')
    row = db.execute('SELECT next_block,checkpoint_hash FROM runs WHERE id=%s', (run_id,)).fetchone()
    start, checkpoint = row
    if start and checkpoint:
        check_hash(rpc, start - 1, checkpoint)
    check_hash(rpc, target, target_hash)
    with ThreadPoolExecutor(max_workers=workers*3) as pool, ThreadPoolExecutor(max_workers=3) as prefetch:
        def fetch_range(lo):
            hi = min(lo + chunk - 1,target)
            header = rpc.block(hi)
            results = list(pool.map(lambda q: rpc.logs(q,lo,hi,cap),queries))
            return lo,hi,header,results
        remaining = iter(range(start,target+1,chunk))
        pending = deque()
        for _ in range(3):
            lo = next(remaining,None)
            if lo is not None:
                pending.append(prefetch.submit(fetch_range,lo))
        while pending:
            lo,hi,header,results = pending.popleft().result()
            next_lo = next(remaining,None)
            if next_lo is not None:
                pending.append(prefetch.submit(fetch_range,next_lo))
            unique = {}
            for query, logs in zip(queries, results):
                for log in logs:
                    if log.get('removed') or not lo <= int(log['blockNumber'], 16) <= hi or not matches(log, query):
                        raise RPCError('RPC returned removed/out-of-range/nonmatching log')
                    key = (log['transactionHash'], int(log['logIndex'], 16))
                    if key in unique and unique[key] != log:
                        raise RPCError('Inconsistent duplicate log')
                    unique[key] = log
            check_hash(rpc, hi, header['hash'])
            raw_rows, movement_rows = [], []
            for log in sorted(unique.values(), key=lambda x: (int(x['blockNumber'],16), int(x['transactionIndex'],16), int(x['logIndex'],16))):
                tx, idx = log['transactionHash'], int(log['logIndex'],16)
                raw_rows.append((run_id, int(log['blockNumber'],16), log['blockHash'], tx,
                                 int(log['transactionIndex'],16), idx, log['address'].lower(), Jsonb(log)))
                movement_rows.extend((run_id,tx,idx,*item) for item in movements(log,wallet,assets))
            with db.transaction():
                with db.cursor() as cursor:
                    cursor.execute('TRUNCATE incoming_logs,incoming_movements')
                    with cursor.copy('COPY incoming_logs FROM STDIN') as copy:
                        for row in raw_rows:
                            copy.write_row(row)
                    with cursor.copy('COPY incoming_movements FROM STDIN') as copy:
                        for row in movement_rows:
                            copy.write_row(row)
                    cursor.execute('INSERT INTO raw_logs SELECT * FROM incoming_logs ON CONFLICT DO NOTHING')
                    cursor.execute('INSERT INTO movements SELECT * FROM incoming_movements ON CONFLICT DO NOTHING')
                db.execute('UPDATE runs SET next_block=%s,checkpoint_hash=%s WHERE id=%s', (hi+1,header['hash'],run_id))
            logging.info('blocks %s..%s / %s; %s wallet logs', lo, hi, target, len(unique))


def verify_receipts(db, rpc, run_id, assets, wallet):
    """Validate source logs, persist full transaction context, catch missing sibling logs."""
    queries = filters(wallet, assets)
    txs = db.execute('SELECT DISTINCT tx_hash,block_number,block_hash FROM raw_logs WHERE run_id=%s ORDER BY block_number,tx_hash', (run_id,)).fetchall()
    block_hashes = {}
    for tx, number, expected_hash in txs:
        if number not in block_hashes:
            block_hashes[number] = rpc.block(number)['hash']
        if block_hashes[number] != expected_hash:
            raise RPCError('Noncanonical source log')
        saved = db.execute('SELECT payload FROM receipts WHERE run_id=%s AND tx_hash=%s', (run_id,tx)).fetchone()
        receipt = saved[0] if saved else rpc.call('eth_getTransactionReceipt', [tx])
        if not receipt or receipt['transactionHash'] != tx or receipt['blockHash'] != expected_hash or int(receipt['status'],16) != 1:
            raise RPCError('Invalid receipt or changed chain')
        relevant = {int(l['logIndex'],16): l for l in receipt['logs'] if any(matches(l,q) for q in queries)}
        stored = dict(db.execute('SELECT log_index,payload FROM raw_logs WHERE run_id=%s AND tx_hash=%s', (run_id,tx)).fetchall())
        if relevant.keys() != stored.keys():
            raise RPCError('eth_getLogs omitted wallet logs present in a receipt')
        for idx, log in stored.items():
            for field in ('address','topics','data','blockHash','transactionHash','logIndex','blockNumber','transactionIndex'):
                if relevant[idx][field] != log[field]:
                    raise RPCError('Log differs from receipt')
        if not saved:
            db.execute('INSERT INTO receipts VALUES (%s,%s,%s)', (run_id,tx,Jsonb(receipt)))


def reconcile(db, rpc, run_id, wallet, assets, target, target_hash):
    check_hash(rpc, target, target_hash)
    calculated = {(c,int(t)): int(n) for c,t,n in db.execute(
        'SELECT contract,token_id,sum(delta) FROM movements WHERE run_id=%s GROUP BY contract,token_id', (run_id,))}
    for c in assets['erc20']:
        calculated.setdefault((c,0),0)
    # A negative prefix means incomplete/inconsistent history, even if final totals agree.
    negative = db.execute('SELECT count(*) FROM wallet_history WHERE run_id=%s AND running_balance<0', (run_id,)).fetchone()[0]
    rows, proofs, tasks = [], [], []
    for contract in sorted(set(c for c,t in calculated)):
        tokens = sorted(t for c,t in calculated if c == contract)
        is1155 = contract in assets['erc1155']
        code = rpc.call('eth_getCode', [contract, hex(target)])
        if code == '0x':
            if is1155 or any(calculated[contract,t] for t in tokens):
                raise RPCError('Token with movements has no code at target block')
            tasks.append((contract,[0],None))
        elif is1155:
            for offset in range(0,len(tokens),200):
                batch = tokens[offset:offset+200]
                data = signature('balanceOfBatch(address[],uint256[])')[:10] + encode(
                    ['address[]','uint256[]'],[[wallet]*len(batch),batch]).hex()
                tasks.append((contract,batch,data))
        else:
            data = signature('balanceOf(address)')[:10] + encode(['address'],[wallet]).hex()
            tasks.append((contract,[0],data))
    def fetch(task):
        contract,tokens,data = task
        if data is None:
            return task,'0x',[0]
        raw = rpc.call('eth_call',[{'to':contract,'data':data},hex(target)])
        if contract in assets['erc1155']:
            values = list(decode(['uint256[]'],bytes.fromhex(raw[2:]))[0])
        else:
            if not re.fullmatch('0x[0-9a-fA-F]{64}',raw):
                raise RPCError('Invalid balanceOf response')
            values = [int(raw,16)]
        if len(values) != len(tokens):
            raise RPCError('balanceOfBatch returned wrong number of results')
        return task,raw,values
    with ThreadPoolExecutor(max_workers=4) as pool:
        for proof_index,(task,raw,values) in enumerate(pool.map(fetch,tasks)):
            contract,tokens,data = task
            is1155 = contract in assets['erc1155']
            source = ('balanceOfBatch' if is1155 else 'balanceOf') if data else 'no_contract_code'
            proofs.append({'contract':contract,'source':source,'block':target,
                           'call_data':data,'call_result':raw,'token_ids':[str(t) for t in tokens]})
            for token,chain_balance in zip(tokens,values):
                balance = calculated[contract,token]
                rows.append({'contract':contract,'asset':assets['erc1155' if is1155 else 'erc20'][contract],
                             'token_id':str(token),'calculated_raw':str(balance),'onchain_raw':str(chain_balance),
                             'difference_raw':str(balance-chain_balance),'source':source,'proof_index':proof_index})
            if proof_index % 50 == 0:
                logging.info('balance proofs %s/%s',proof_index+1,len(tasks))
    check_hash(rpc, target, target_hash)
    ok = negative == 0 and all(r['difference_raw'] == '0' for r in rows)
    with db.transaction():
        db.execute('DELETE FROM reconciliation WHERE run_id=%s',(run_id,))
        db.execute('DELETE FROM rpc_proofs WHERE run_id=%s',(run_id,))
        with db.cursor() as cursor:
            cursor.executemany('INSERT INTO rpc_proofs VALUES (%s,%s,%s)',
                               [(run_id,i,Jsonb(proof)) for i,proof in enumerate(proofs)])
            cursor.executemany('INSERT INTO reconciliation VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                               [(run_id,r['contract'],int(r['token_id']),int(r['calculated_raw']),int(r['onchain_raw']),int(r['difference_raw']),'proof:'+str(r['proof_index']),r['onchain_raw']) for r in rows])
        db.execute('UPDATE runs SET status=%s WHERE id=%s',('matched' if ok else 'mismatch',run_id))
    return ok, negative, rows, proofs


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--wallet', default=DEFAULT_WALLET)
    p.add_argument('--assets',type=Path,default=ROOT/'assets.json')
    p.add_argument('--run-id',help='Resume an existing run; snapshot and scope must match')
    p.add_argument('--rescan',action='store_true',help='Repeat all ranges for this run, retaining deduplicated logs')
    p.add_argument('--verify-receipts',action='store_true',help='Audit every discovered transaction receipt (slower)')
    p.add_argument('--block',type=int,help='Snapshot block; default is finalized')
    p.add_argument('--chunk',type=int,default=100000)
    p.add_argument('--result-cap',type=int,default=1000)
    p.add_argument('--workers',type=int,default=2)
    p.add_argument('--report',type=Path,default=ROOT/'report.json')
    a = p.parse_args()
    if min(a.chunk,a.result_cap,a.workers) < 1 or (a.block is not None and a.block < 0):
        p.error('Ranges and limits must be positive; block must be nonnegative')
    wallet = address(a.wallet)
    if wallet == ZERO:
        p.error('Zero address is not a wallet')
    assets = json.loads(a.assets.read_text())
    assets = {kind: {address(c):name for c,name in assets[kind].items()} for kind in ('erc20','erc1155')}
    if set(assets['erc20']) & set(assets['erc1155']):
        p.error('Contract cannot have two token standards')
    if not os.environ.get('POLYGON_RPC_URL') or not os.environ.get('DATABASE_URL'):
        p.error('Set POLYGON_RPC_URL and DATABASE_URL')
    rpc = RPC(os.environ['POLYGON_RPC_URL'])
    if int(rpc.call('eth_chainId',[]),16) != 137:
        raise RPCError('RPC is not Polygon mainnet (137)')
    config = {'wallet':wallet,'assets':assets,'chain_id':137,'start_block':0,'version':1}
    with psycopg.connect(os.environ['DATABASE_URL'],autocommit=True) as db:
        db.execute((ROOT/'schema.sql').read_text())
        old = db.execute('SELECT config,target_block,target_hash,target_timestamp FROM runs WHERE id=%s',(a.run_id,)).fetchone() if a.run_id else None
        if a.run_id and not old:
            p.error('Unknown run ID')
        if old:
            old_config,target,target_hash,stamp = old
            if old_config != config or (a.block is not None and a.block != target):
                p.error('Resume configuration does not match saved run')
            run_id = a.run_id
        else:
            header = rpc.block(a.block if a.block is not None else 'finalized')
            target,target_hash,stamp = int(header['number'],16),header['hash'],int(header['timestamp'],16)
            run_id = hashlib.sha256(json.dumps([config,target_hash],sort_keys=True).encode()).hexdigest()[:24]
            db.execute('INSERT INTO runs(id,config,target_block,target_hash,target_timestamp) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                       (run_id,Jsonb(config),target,target_hash,stamp))
        locked = db.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))',(run_id,)).fetchone()[0]
        if not locked:
            raise RuntimeError('This run is already active in another process')
        if a.rescan:
            check_hash(rpc,target,target_hash)
            db.execute("UPDATE runs SET next_block=0,checkpoint_hash=NULL,status='scanning' WHERE id=%s",(run_id,))
        logging.info('run_id=%s target=%s hash=%s',run_id,target,target_hash)
        scan(db,rpc,run_id,wallet,assets,target,target_hash,a.chunk,a.result_cap,a.workers)
        if a.verify_receipts:
            verify_receipts(db,rpc,run_id,assets,wallet)
        ok,negative,rows,proofs = reconcile(db,rpc,run_id,wallet,assets,target,target_hash)
        count = db.execute('SELECT count(*) FROM raw_logs WHERE run_id=%s',(run_id,)).fetchone()[0]
        report = {'run_id':run_id,'wallet':wallet,'chain_id':137,'block_number':target,'block_hash':target_hash,
                  'block_timestamp':stamp,'start_block':0,'all_balances_match':ok,'negative_prefixes':negative,
                  'source_log_count':count,'receipts_verified':a.verify_receipts,'scope':assets,'balances':rows,'rpc_proofs':proofs,
                  'completeness_note':'Endpoint must return complete eth_getLogs results. Balance equality alone does not prove historical completeness.'}
        a.report.parent.mkdir(parents=True,exist_ok=True)
        temp = a.report.with_suffix('.tmp')
        temp.write_text(json.dumps(report,indent=2)+'\n')
        temp.replace(a.report)
        print(json.dumps({'report':str(a.report),'run_id':run_id,'all_balances_match':ok,'source_logs':count}))
        return 0 if ok else 2


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    try:
        raise SystemExit(main())
    except (RPCError,ValueError) as exc:
        logging.error('%s',exc)
        raise SystemExit(1)
