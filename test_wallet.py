import json
from pathlib import Path
from eth_abi import encode
import pytest
import httpx
from wallet import BATCH, SINGLE, TRANSFER, ZERO, DEFAULT_WALLET, movements, topic_address, filters, matches, RPC, RangeLimit

ASSETS = json.loads((Path(__file__).parent/'assets.json').read_text())
CTF = next(iter(ASSETS['erc1155']))
USD = next(iter(ASSETS['erc20']))
W = DEFAULT_WALLET
OTHER = '0x'+'11'*20


def log(sig, sender, recipient, ids=None, amounts=None):
    erc20 = sig == TRANSFER
    topics = [sig] + ([] if erc20 else [topic_address(OTHER)]) + [topic_address(sender),topic_address(recipient)]
    if erc20:
        types,values = ['uint256'],[amounts[0]]
    elif sig == SINGLE:
        types,values = ['uint256','uint256'],[ids[0],amounts[0]]
    else:
        types,values = ['uint256[]','uint256[]'],[ids,amounts]
    return {'address': USD if erc20 else CTF, 'topics':topics,'data':'0x'+encode(types,values).hex()}


def test_mint_burn_and_self_transfer():
    assert movements(log(SINGLE,ZERO,W,[2**256-1],[5]),W,ASSETS)[0][-1] == 5
    assert movements(log(SINGLE,W,ZERO,[7],[3]),W,ASSETS)[0][-1] == -3
    assert movements(log(SINGLE,W,W,[7],[3]),W,ASSETS)[0][-1] == 0


def test_batch_duplicates_and_uint256_precision():
    rows = movements(log(BATCH,OTHER,W,[9,9,2**256-1],[1,2,2**256-1]),W,ASSETS)
    assert [r[0] for r in rows] == [0,1,2]
    assert sum(r[-1] for r in rows) == 2**256+2


def test_erc20_and_both_directions():
    for sender,recipient,expected in [(ZERO,W,123),(W,OTHER,-123),(W,W,0)]:
        value = log(TRANSFER,sender,recipient,amounts=[123])
        assert movements(value,W,ASSETS)[0][-1] == expected
        assert any(matches(value,q) for q in filters(W,ASSETS))


def test_batch_bad_length_fails():
    with pytest.raises(ValueError):
        movements(log(BATCH,ZERO,W,[1,2],[3]),W,ASSETS)


def test_unrelated_log_fails():
    with pytest.raises(ValueError):
        movements(log(SINGLE,ZERO,OTHER,[7],[3]),W,ASSETS)


def test_range_split_has_no_gap_and_single_block_fallback():
    class Limited(RPC):
        def __init__(self):
            self.seen = []
        def call(self,method,params):
            lo,hi = int(params[0]['fromBlock'],16),int(params[0]['toBlock'],16)
            if hi > lo:
                raise RangeLimit()
            self.seen.append(lo)
            return [{'block':lo}]
    rpc = Limited()
    assert rpc.logs({},0,8,10) == [{'block':i} for i in range(9)]
    assert rpc.seen == list(range(9))
    rpc.block_logs = lambda q,n: [{'fallback':n}]
    assert rpc.logs({},5,5,1) == [{'fallback':5}]


def test_limit_in_error_data_splits_instead_of_failing():
    rpc = RPC('https://rpc.invalid')
    rpc.client.close()
    rpc.client = httpx.Client(transport=httpx.MockTransport(lambda req:
        httpx.Response(200,json={'jsonrpc':'2.0','id':1,'error':{
            'code':-32602,'message':'invalid params',
            'data':'Query returned more than 20000 results. Try with this block range'}})))
    with pytest.raises(RangeLimit):
        rpc.call('eth_getLogs',[{}])
