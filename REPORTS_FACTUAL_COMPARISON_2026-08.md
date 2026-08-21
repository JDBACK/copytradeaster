# Фактологическое сравнение параллельных отчётов по AsterDEX

Дата проверки: 19 августа 2026 года.

## Область сравнения

Сравнивались только вопросы исходного исследования:

- актуальная версия Aster API и создание V3 Agent/API Wallet;
- права ключа, balance/positions/leverage/margin;
- открытие, закрытие, partial close, Stop Loss, Take Profit и их изменение;
- наблюдение master account и копирование через REST/WebSocket;
- testnet/demo;
- фактические ограничения Aster.

Не оценивались предлагаемый язык, Telegram/Mini App UX, схема БД, сроки разработки, VPS, библиотеки, юридические вопросы и общие советы по реализации.

## Полнота чтения

Все девять Markdown-файлов в `reports/` прочитаны целиком, а не только по заголовкам или поисковым совпадениям:

| Файл | Строк | Слов |
|---|---:|---:|
| `01_platform.md` | 352 | 4 610 |
| `02_api_keys.md` | 366 | 3 838 |
| `03_onchain_visibility.md` | 234 | 3 326 |
| `04_github_scan.md` | 175 | 2 142 |
| `05_copytrade_architecture.md` | 410 | 5 025 |
| `06_implementation_design.md` | 764 | 7 289 |
| `07_trade_format.md` | 181 | 1 174 |
| `ASTER_COPY_CANONICAL_REPORT.md` | 2 404 | 6 968 |
| `FINAL_REPORT.md` | 110 | 1 813 |
| **Всего** | **4 996** | **36 185** |

Базой сравнения был `ASTER_API_RESEARCH_2026-08.md`: до этого аудита 497 строк / 3 647 слов, после включения подтверждённых дополнений — 581 строка / 4 678 слов.

## Общий вывод

Наш исходный отчёт уже был точнее большинства параллельных материалов по четырём критичным пунктам:

1. использовал V3 Agent/EIP-712 как текущую модель, а V1 HMAC — только как legacy;
2. различал обычный conditional order и Strategy Order;
3. правильно указывал, что standalone STOP/TP нельзя менять обычным `PUT /order`;
4. не обещал полную Strategy parity testnet без signed-проверки.

Новые отчёты всё же дали семь полезных направлений для усиления. Самым сильным оказался `ASTER_COPY_CANONICAL_REPORT.md`: он лучше остальных разделяет подтверждённый API contract, продуктовую политику и то, что требует экспериментальной проверки. `02_api_keys.md` лучше всего собрал lifecycle Agent и account history. `03_onchain_visibility.md` наиболее понятно объяснил, почему публичное наблюдение wallet не заменяет private master stream.

После официальной перепроверки в основной отчёт внесены:

1. разные signing contexts для trading и Agent registration;
2. Agent lifecycle: approve/update/get/delete;
3. точное значение account-level, а не signer-level мониторинга;
4. семидневные окна истории и исчезновение старых canceled/unfilled orders;
5. ограничения Strategy Orders, `closePosition` и `priceProtect`;
6. реальный scope `newClientOrderId` и отличие от transfer `clientTranId`;
7. возможности и ограничения sub-accounts, включая постоянно отключённый withdrawal у их API keys.

## Сравнение каждого файла

### `01_platform.md`

Что полезно:

- хорошо отделяет публичную on-chain видимость от приватного account data;
- обращает внимание на hidden orders/positions и sub-accounts;
- верно показывает, что приватность чужого wallet не мешает читать собственный account через авторизованный API.

Что изложено лучше нашего прежнего текста:

- понятнее объяснён контекст privacy: «децентрализованная биржа» не означает, что сделки любого master wallet обязательно публичны.

Что не перенесено:

- почти весь обзор платформы, токена, команды, объёмов и новостей находится вне заданного API scope;
- API в одном месте описан как текущий Binance-compatible HMAC SHA256. На август 2026 это только V1 legacy; новый бот должен использовать V3 Agent/EIP-712;
- упоминание sub-accounts по roadmap слабее текущей отдельной официальной спецификации.

Вердикт: полезен как контекст privacy, но не как источник текущего API contract.

### `02_api_keys.md`

Что полезно:

- наиболее полный список `approveAgent`, `updateAgent`, `GET/DELETE /agent`;
- правильно замечено, что официального audit-log «какой signer сделал действие» нет;
- хорошо собраны `allOrders`, `userTrades`, `income`, сроки истории и параметры Agent;
- полезно выделена дополнительная wallet-подпись для EVM withdrawal.

Что изложено лучше нашего прежнего текста:

- lifecycle Agent и различие между account history и Agent audit объяснены существенно подробнее;
- показано, что `GET /agent` возвращает expiry, whitelist и permission flags.

Что ошибочно или устарело:

- таблица использует старые Strategy routes `/strategyOrder` и `/strategyOrder/history`; актуальные query routes — `/strategyOpenOrder` и `/strategyHistoryOrder`;
- `TRADE_PRO` назван персональным user-data событием. В текущем V3 reference это публичная symbol-подписка `<symbol>@tradepro`;
- testnet назван идентичным production, хотя Strategy sections отсутствуют в testnet reference, а product testnet не поддерживает deposits/withdrawals/internal transfers;
- `fapi3.asterdex.com` оставлен как равноправный кандидат; публичный probe 19.08.2026 дал `fapi` HTTP 200 и `fapi3` HTTP 403;
- отсутствие faucet объявлено пробелом, хотя официальный testnet product page указывает 1 000 test USDT + 1 000 test ASTER раз в день;
- защита withdrawal обобщена на все сети. Для EVM `userSignature` обязателен, но текущая Solana-спека пока делает его optional и обещает будущий enforcement;
- фраза «исторические трейды будут трассируемы» — обещание будущего, не текущий audit mechanism.

Вердикт: второй по полезности файл; lifecycle и account history приняты после коррекции endpoint names и withdrawal semantics.

### `03_onchain_visibility.md`

Что полезно:

- чётко показывает, что private User Data Stream — основной live-источник для собственного master account;
- публичный RPC пригоден только как optional source для privacy-off accounts и не даёт гарантированного live order stream;
- правильно отделяет viewer pass от торгового API key.

Что изложено лучше нашего прежнего текста:

- наиболее наглядно закрывает ложную альтернативу «Aster DEX децентрализован, значит можно вообще не получать доступ мастера».

Что требует осторожности:

- тест нулевого адреса не доказывает поведение реального privacy-enabled account;
- часть privacy/viewer-pass утверждений опирается на сторонний AsterScan и поисковый кэш;
- способность viewer pass раскрывать полную realtime-историю не подтверждена как API contract.

Вердикт: в основной отчёт взят только узкий подтверждённый вывод — public RPC не заменяет private master stream.

### `04_github_scan.md`

Что полезно:

- подтверждает официальный статус `asterdex/api-docs`, `aster-skills-hub` и `API-demo`;
- правильно исключает V1-only clients как фундамент новой интеграции.

Что изложено лучше нашего прежнего текста:

- ничего существенного именно по API capabilities; файл лучше решает отдельный вопрос выбора codebase.

Что не перенесено:

- языки, лицензии, звёзды, библиотеки и готовые боты находятся вне текущего factual scope;
- заявления сторонних SDK не использовались как доказательство поведения биржи.

Вердикт: новых фактов для API-отчёта нет.

### `05_copytrade_architecture.md`

Что полезно:

- правильно подчёркивает `503 = UNKNOWN`, account/IP rate limits и необходимость REST reconciliation;
- обращает внимание на различие `newClientOrderId` и `clientTranId`, хотя затем частично смешивает их.

Что изложено лучше нашего прежнего текста:

- хорошо собраны типовые последствия 503/timeout и необходимость query before retry.

Что ошибочно или является не API-фактом:

- формула делит position notional на leverage при фиксированном риске на Stop. Это неверно: leverage меняет required margin и liquidation distance, но не loss до Stop;
- `clientTranId` представлен как order idempotency key на семь дней. В Aster это transfer ID; `newClientOrderId` для order гарантирован уникальным только среди open orders;
- из уникального nonce сделан вывод, что все HTTP requests Agent обязательно строго последовательны. Протокол требует уникальности nonce, а не единственного in-flight request;
- «read-only key master» предполагается без подтверждения доступа такого Agent к USER_DATA/listenKey;
- публичные Hyperliquid-паттерны нельзя автоматически переносить на privacy-first Aster.

Вердикт: новых Aster capabilities почти нет; полезны только уточнения retry/idempotency, уже исправленные в основном отчёте.

### `06_implementation_design.md`

Что полезно:

- корректно перечисляет основные REST/WS endpoints;
- хорошо показывает разницу One-way/Hedge для `reduceOnly`;
- признаёт, что Strategy testnet и read-only Agent требуют проверки.

Что изложено лучше нашего прежнего текста:

- отдельные API edge cases собраны в одном месте, но основная ценность файла — проектирование, которое пользователь просил не сравнивать.

Что ошибочно или не должно считаться фактом Aster:

- в одном месте смешаны trading `chainId=1666/714` и registration `56/101`; это два разных signing contexts;
- `priceProtect=true` выбран как безусловно безопасный Stop default, хотя спецификация говорит `false` по умолчанию и допускает задержку trigger при большом MARK/CONTRACT divergence;
- утверждается, что `canWithdraw` нельзя проверить через API, хотя `GET /fapi/v3/agent` возвращает этот флаг;
- `newClientOrderId` местами наделяется семидневной дедупликацией, которой документация не гарантирует;
- stop followers задаётся независимо от Stop master — это продуктовая политика, а не API-возможность;
- фильтровать liquidation master и оставлять follower exposure — также продуктовая политика, не требование Aster.

Вердикт: endpoint checklist полезен, но factual report не должен наследовать его продуктовые решения.

### `07_trade_format.md`

Что полезно:

- хорошо показывает, что «позиция + SL + TP» может быть несколькими standalone orders либо Strategy Order с sub-orders;
- полезно выделяет late Stop и несколько legs одной торговой идеи.

Что изложено лучше нашего прежнего текста:

- визуально понятнее представлена многоногая структура сделки.

Что ошибочно или не подтверждено:

- обычному `PUT /fapi/v3/order` приписано изменение `stopPrice`; текущая спека допускает modify только `LIMIT` с новыми `quantity` и `price`;
- заявлено «событие стратегии» для ручного update; отдельное Strategy-update WS event не документировано;
- перенос Stop как одинаковой процентной дистанции — продуктовая политика. Если Stop master является уровнем инвалидирования идеи, безопаснее сохранить абсолютный market level и изменить follower quantity;
- время сборки пакета «0.2–2 с» не подтверждено официальным SLA.

Вердикт: сильное объяснение trade legs, но ключевой механизм standalone Stop modification в файле неверен.

### `ASTER_COPY_CANONICAL_REPORT.md`

Что полезно:

- лучший документ по строгости: отдельно маркирует API fact, product decision и required experiment;
- правильно различает trading domain `1666/714` и registration `signatureChainId=56/101`;
- точно описывает Strategy weight 50 и запрет IOC/FOK для strategy sub-orders;
- правильно объясняет `priceProtect`, scope `newClientOrderId`, absolute Stop level и risk cap;
- включает current sub-account capability и необходимость empirical Strategy/UI traces.

Что изложено лучше нашего прежнего текста:

- signing contexts, `priceProtect`, idempotency scope и sub-account isolation были изложены существенно лучше и стали главными источниками улучшения нашего отчёта;
- сильнее проведена граница между документированным и тем, что нельзя обещать до signed test.

Что скорректировано:

- в перечне user events используется название `userDataStreamExpired`; фактическое поле `e` в текущем mainnet/testnet V3 payload — `listenKeyExpired`;
- схема «create new Stop → cancel old» описана как предпочтительная, но сама же требует testnet-проверки. В основной отчёт она не объявлена гарантированно безопасной;
- отдельные архитектурные, коммерческие и юридические разделы находятся вне текущего scope.

Вердикт: лучший параллельный отчёт. Большинство новых подтверждённых дополнений пришло из него, но каждый пункт был перепроверен по текущей официальной документации.

### `FINAL_REPORT.md`

Что полезно:

- удачно сжимает выводы всех этапов;
- чётко отвечает, что собственный master stream решает проблему Aster privacy.

Что изложено лучше нашего прежнего текста:

- краткая связка «private master stream + follower V3 agents» сформулирована очень ясно.

Что ошибочно или унаследовано из ранних этапов:

- программное создание сведено к `/approveAgent`, хотя текущий main Futures reference добавляет публичный `registerAndApproveAgent`, а Agent-management reference сохраняет approve/update/delete/get;
- формула `now_ms × 1e6` даёт не микросекунды, если `now_ms` действительно milliseconds;
- `TRADE_PRO` снова назван user-data событием;
- nonce трактуется как требование полной последовательности requests;
- `priceProtect=true` предложен как однозначно безопасный Stop default;
- chainId оставлен открытым вопросом, хотя официальный reference уже разделяет `1666/714` и `56/101`;
- faucet описан без подтверждённых exact amounts/frequency.

Вердикт: хороший executive summary, но менее точный, чем наш обновлённый отчёт и канонический документ.

## Что именно добавлено в основной отчёт

### 1. Agent signing и lifecycle

Добавлено в раздел `1. Актуальная версия и модель ключей`:

- trading domain `1666/714`;
- registration `signatureChainId=56/101`;
- `registerAndApproveAgent` weight 50, IP/CIDR delimiter и требование whitelist при withdrawal;
- `approveAgent`, `updateAgent`, `GET/DELETE /agent`;
- `GET /agent` permission fields;
- read-only Agent оставлен как signed test, а не обещанная capability.

Официальные основания: [Futures V3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md#register-and-approve-agent-public), [Agent management](https://asterdex.github.io/aster-api-website/asterCode/endpoints/).

### 2. Withdrawal semantics

Добавлено различие:

- EVM V3 withdrawal требует отдельного owner `userSignature`;
- Solana signature пока optional в текущей документации;
- поэтому обязательная защита copy key — `canWithdraw=false`, а не предположение о второй подписи для любой сети.

Официальное основание: [V3 Deposit & Withdrawal](https://asterdex.github.io/aster-api-website/futures-v3/deposit%26withdrawal/).

### 3. Sub-accounts

Добавлены текущие mainnet-факты:

- собственные assets/positions/API keys;
- 10–50 sub-accounts по VIP/MM tier;
- до 10 V3 keys на sub-account;
- withdrawal permission постоянно disabled;
- direct withdrawal отсутствует;
- deposits/internal transfers/freeze semantics;
- testnet parity не предполагается без проверки.

Официальное основание: [Sub Accounts](https://docs.asterdex.com/trading/sub-accounts).

### 4. Stops и Strategy Orders

Добавлены:

- Strategy weight 50;
- запрет IOC/FOK у Strategy sub-orders;
- точные ограничения `closePosition=true`;
- default `priceProtect=false` и риск задержки trigger при `true`;
- standalone Stop по-прежнему только cancel+new, Strategy Stop — `updateStrategyOrder`.

### 5. Что именно наблюдает master API

Добавлено прямое объяснение:

- user stream и REST history относятся к account;
- ручные действия и действия любых Agents account видны вместе;
- отдельного signer audit-log нет;
- `TRADE_PRO` не является user-data event;
- account history имеет семидневные query/retention ограничения;
- свой event ledger нужен для долгого восстановления.

### 6. Order idempotency

Добавлено:

- `newClientOrderId` гарантирован уникальным только среди open orders;
- transfer `clientTranId` — отдельное поле с семидневной уникальностью;
- exchange client ID не заменяет долговременную дедупликацию сервиса.

### 7. Дополнительные signed tests

В testnet checklist добавлены:

- доступ Agent с `canPerpTrade=false` к USER_DATA/listenKey;
- `priceProtect=false/true` и `triggerProtect`;
- разделение trading и registration signing contexts.

## Что было перепроверено после чтения

1. Mainnet routes `registerAndApproveAgent`, `approveAgent`, `updateAgent`, `GET/DELETE /agent` существуют: без обязательных параметров возвращают 400, а не 404.
2. Current Futures V3 reference действительно требует `signatureChainId=56/101` только для registration и запрещает там fixed `1666`.
3. `placeStrategyOrder/updateStrategyOrder` имеют weight 50; IOC/FOK не поддерживаются.
4. `priceProtect` default — false; при true действует `triggerProtect` MARK/CONTRACT divergence gate.
5. `newClientOrderId` — unique among open orders; `clientTranId` seven-day rule относится к transfer.
6. Event section называется «User Data Stream Expired», но фактический payload — `e=listenKeyExpired`.
7. `TRADE_PRO` — public `<symbol>@tradepro` subscription.
8. EVM withdrawal требует owner signature; Solana signature сейчас optional.
9. Текущая live-страница Sub Accounts уже говорит «all VIP levels» и 10–50 sub-accounts. Старый search cache с требованием VIP 2 и максимумом 15 устарел.
10. Strategy testnet execution, manual UI Strategy updates и read-only Agent access по-прежнему нельзя считать подтверждёнными без credentials.

## Итог

Да, в параллельных отчётах были факты, которых не хватало. Они уже включены в `ASTER_API_RESEARCH_2026-08.md`. При этом основной вывод не изменился: Aster Futures V3 покрывает core-функции копитрейдинга, а главные непроверенные зоны остаются signed Strategy behavior на testnet, ручные UI Strategy updates и фактический USER_DATA access trade-disabled Agent.
