CREATE TABLE IF NOT EXISTS runs (
    id text PRIMARY KEY,
    config jsonb NOT NULL,
    target_block bigint NOT NULL,
    target_hash text NOT NULL,
    target_timestamp bigint NOT NULL,
    next_block bigint NOT NULL DEFAULT 0,
    checkpoint_hash text,
    status text NOT NULL DEFAULT 'scanning',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS raw_logs (
    run_id text NOT NULL REFERENCES runs(id),
    block_number bigint NOT NULL,
    block_hash text NOT NULL,
    tx_hash text NOT NULL,
    tx_index integer NOT NULL,
    log_index integer NOT NULL,
    contract text NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (run_id, tx_hash, log_index)
);
CREATE TABLE IF NOT EXISTS movements (
    run_id text NOT NULL,
    tx_hash text NOT NULL,
    log_index integer NOT NULL,
    item_index integer NOT NULL,
    contract text NOT NULL,
    token_id numeric(78,0) NOT NULL,
    sender text NOT NULL,
    recipient text NOT NULL,
    amount numeric(78,0) NOT NULL CHECK (amount >= 0),
    delta numeric(78,0) NOT NULL,
    PRIMARY KEY (run_id, tx_hash, log_index, item_index),
    FOREIGN KEY (run_id,tx_hash,log_index) REFERENCES raw_logs(run_id,tx_hash,log_index)
);
CREATE INDEX IF NOT EXISTS movements_asset ON movements(run_id,contract,token_id);
CREATE TABLE IF NOT EXISTS receipts (
    run_id text NOT NULL REFERENCES runs(id),
    tx_hash text NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (run_id,tx_hash)
);
CREATE TABLE IF NOT EXISTS reconciliation (
    run_id text NOT NULL REFERENCES runs(id),
    contract text NOT NULL,
    token_id numeric(78,0) NOT NULL,
    calculated numeric NOT NULL,
    onchain numeric(78,0) NOT NULL,
    difference numeric NOT NULL,
    call_data text NOT NULL,
    call_result text NOT NULL,
    PRIMARY KEY (run_id,contract,token_id)
);
CREATE TABLE IF NOT EXISTS rpc_proofs (
    run_id text NOT NULL REFERENCES runs(id),
    proof_index integer NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (run_id,proof_index)
);
CREATE OR REPLACE VIEW wallet_history AS
SELECT m.*, l.block_number, l.block_hash, l.tx_index,
       sum(m.delta) OVER (
          PARTITION BY m.run_id,m.contract,m.token_id
          ORDER BY l.block_number,l.tx_index,l.log_index,m.item_index
          ROWS UNBOUNDED PRECEDING
       ) AS running_balance
FROM movements m JOIN raw_logs l USING (run_id,tx_hash,log_index);
