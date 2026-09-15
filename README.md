# RM Invest — история кошелька через Polygon RPC

Адрес: `0x46b353667fd7d846af3bbeda6584b0e5b883d3de`.

Python + PostgreSQL. Данные кошелька получаются исключительно стандартными
JSON-RPC методами Polygon. Нет Polymarket API, Polygonscan API, The Graph
или готового индексатора.

## Результат выполненного запуска

- Snapshot: блок **93,845,241**, `2026-09-15T11:48:15+00:00`.
- Исходных логов: **2,236,962**.
- Проверенных балансов: **420,295**, расхождений: **0**.
- Отрицательных промежуточных балансов: **0**.
- Ненулевых ERC1155-позиций: **392,775**.
- pUSD: **127.561028** (127561028 минимальных единиц, decimals=6 проверено через RPC).
- 9 тестов пройдены, включая интеграционные проверки PostgreSQL 17.
- Полный receipt-аудит выключен; сверка опирается на Transfer-логи и state calls.

`summary.json` — краткий результат; `report.json` — все балансы и исходные
запросы/ответы RPC для сверки. Дамп `wallet-postgresql.dump` поставляется отдельно.

Восстановление дампа в отдельную пустую БД с PostgreSQL 17:

```sh
createdb restored_wallet
pg_restore --no-owner --no-acl -d restored_wallet wallet-postgresql.dump
```

## Запуск

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
docker compose up -d --wait
export DATABASE_URL='postgresql://wallet:local_wallet_only@127.0.0.1:55439/wallet'
export POLYGON_RPC_URL='https://polygon.gateway.tenderly.co'
.venv/bin/python wallet.py --chunk 100000 --result-cap 20000
```

Публичный RPC может ограничивать историю или нагрузку: замените URL своим
archive RPC с полной поддержкой исторического `eth_getLogs` и `eth_call`.
Пароль в compose предназначен только для локальной БД на loopback-интерфейсе.
Если локальный контейнер `rm-invest-wallet-pg` уже работает на порту 55439,
повторный `docker compose up` не нужен.

По умолчанию фиксируется блок `finalized`; `--block N` задаёт конкретный блок.
Отсутствие поддержки `finalized` не заменяется молча на `latest`.
В логах печатается `run_id`. Возобновление:

```sh
.venv/bin/python wallet.py --run-id RUN_ID --chunk 100000 --result-cap 20000
.venv/bin/pytest -q
# Полная проверка с PostgreSQL:
TEST_DATABASE_URL="$DATABASE_URL" .venv/bin/pytest -q
```

`report.json` создаётся после полного сканирования и сверки. Содержит номер,
хеш, timestamp блока, scope, исходные целочисленные балансы и разницы.
Код завершения: 0 — балансы совпали; 2 — расхождение; 1 — ошибка.
Числа в JSON записываются строками, чтобы не потерять uint256 в JavaScript.

## Как считается

1. Проверка `eth_chainId == 137`, фиксация блока и его хеша.
2. Сканирование с блока 0 до snapshot включительно. Отдельные запросы входящих
   и исходящих `Transfer`, `TransferSingle`, `TransferBatch` по indexed topics.
3. Разбиение диапазона при лимитах RPC. При переполнении одного блока читаются
   receipts всех транзакций этого блока. Ошибки не превращаются в пустые результаты.
4. Дедупликация по `(run_id, transactionHash, logIndex)`; batch разворачивается
   с `item_index`, сохраняя повторяющиеся token ID. Self-transfer даёт ноль.
5. COPY во временные таблицы и одна SQL-транзакция сохраняют логи, движения
   и checkpoint. Повторный запуск
   не удваивает суммы. PostgreSQL advisory lock не допускает два загрузчика одного run.
6. Опционально `--verify-receipts`: сохраняются полные receipts найденных
   транзакций, проверяется соответствие логам и canonical block hash.
   События биржи и адаптеров доступны в receipts после этого аудита.
7. Баланс = `SUM(in - out)`. Проверяются также отрицательные промежуточные балансы.
8. Для каждого встреченного ERC1155 token ID, включая обнулённые позиции,
   вызывается `balanceOfBatch` пакетами по 200 ID. Для каждого ERC20 из scope — `balanceOf(wallet)`.
   Все вызовы идут на том же блоке. Хеш блока проверяется до и после сверки.

Split, merge, redeem, комиссии и neg-risk conversion меняют баланс через переводы,
mint и burn. Их бизнес-события не прибавляются повторно к суммам переводов.
Отдельной классификации сделок, PnL, названий рынков и незавершённых ордеров здесь нет.
Журнал представляет историю движения активов, а не копию интерфейса Polymarket.

## PostgreSQL

`raw_logs` — исходные логи; `movements` — движения в минимальных единицах;
`receipts` — полный контекст найденных транзакций; `runs` — scope и checkpoints;
`reconciliation` — сверка; `rpc_proofs` — исходные ответы balanceOf/Batch;
`wallet_history` — история с накопленным балансом. Поле `reconciliation.call_data`
содержит ссылку `proof:N` на `rpc_proofs.proof_index`, `call_result` —
декодированный целочисленный результат для одного актива.
Token ID и amount используют `numeric(78,0)`, суммирование — SQL numeric,
декодирование — Python int. Float не используется.

```sql
SELECT block_number, tx_hash, log_index, item_index,
       contract, token_id, sender, recipient, amount, delta, running_balance
FROM wallet_history
WHERE run_id = 'RUN_ID'
ORDER BY block_number, tx_index, log_index, item_index;

SELECT contract, token_id, calculated, onchain, difference
FROM reconciliation WHERE run_id = 'RUN_ID'
ORDER BY contract, token_id;
```

## Границы и проверяемость

- Scope перечислен в `assets.json`: CTF, PositionManager, USDC.e, native USDC,
  pUSD, neg-risk wrapped collateral. Можно явно добавить контракты.
- Восстанавливается именно указанный адрес; связанные EOA/Safe/proxy не
  объединяются автоматически. POL, газ, NFT других контрактов и произвольные
  сторонние ERC20 не входят в scope.
- При изменении сохранённого хеша программа останавливается: нужен новый run.
  Автоматического отката незавершённой ветки нет.
- Совпадение итогов не доказывает полноту истории: RPC теоретически может
  пропустить пару взаимно компенсирующих переводов. Опциональная receipt-проверка обнаруживает
  пропуски внутри найденных транзакций, но не целиком пропущенные транзакции.
  Нужен RPC с гарантией полной выдачи логов; для более сильного аудита — повторный
  прогон через независимый RPC или все receipts с верификацией receiptsRoot.
- Ненайденные ERC1155 ID не перечисляются методом balanceOf: полнота их обнаружения
  опирается на полные Transfer-логи с genesis. Это явно не криптографическое
  доказательство полноты от недоверенного RPC.
- `finalized` означает snapshot на время RPC-запроса. Проверяйте его timestamp:
  отстающий RPC не подтверждает состояние на сегодняшнюю дату.

## Первичные источники контрактов и ABI

- [Polymarket: актуальные адреса](https://docs.polymarket.com/resources/contracts)
- [Polymarket: адреса V1/V2 и аудит](https://github.com/Polymarket/contract-security)
- [Gnosis ConditionalTokens](https://github.com/gnosis/conditional-tokens-contracts/blob/master/contracts/ConditionalTokens.sol)
- [ERC1155](https://eips.ethereum.org/EIPS/eip-1155)
- [Circle: USDC addresses](https://developers.circle.com/stablecoins/usdc-contract-addresses)

Документация используется для адресов и формата событий, а не как источник
транзакций или балансов кошелька.
