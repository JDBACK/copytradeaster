# Ответ на merge-review ветки `codex/miniapp-live-updates` (2026-09-10)

Ревьюированное дерево: `fb06825` против `main af757d0` — 25 коммитов / 90 файлов.
Текущий HEAD: `6d50fe7` — 9 коммитов поверх ревьюенного дерева; diff против `main`: **99 файлов, +9652 / −597**, из них `src/` — 16 файлов.

После `24ffda8` (последний коммит с правками `src/`) рабочий код не менялся: коммиты `3a316b5`, `f7238d1`, `6225549`, `6d50fe7` — только тесты и документация живой кампании. Ниже: находка → что изменено → чем доказано → статус.

## P1

### 1. History терял сделку при смене HEDGE ↔ ONE_WAY и всё равно двигал boundary

**Причина.** Принятый boundary хранит seeds своей книги. После смены режима authoritative ending описывает уже другую книгу, поэтому полный round-trip новой книги отбрасывался целиком, boundary уходил вперёд, а dirty консьюмился.

**Правка** (`9eaddbb`, `history/projection.py`). Расхождение набора ног ending и boundary распознаётся как смена книги: replay идёт от accepted origin с seeds, выведенными walk-back'ом по удержанным филлам (`_derived_seeds`), origin переанкоривается на новую книгу. Набор `affected` ограничен книгой replay, поэтому снятая книга сохраняет свои эпизоды и `public_ref`.

**Доказательство.** Unit-регрессии `tests/unit/history/test_capture.py` (обе падают при откате фикса). Офлайн-стенд на fake-exchange: до фикса два `BOTH`-филла остаются без аллокации при ушедшем вперёд boundary, после — размечены. Живьём — новый `tests/testnet/test_history_mode_switch_live.py`: реальная смена `AsterClient.set_position_mode` на `copyc2` HEDGE → ONE_WAY → HEDGE, после каждого round-trip production `HistoryCapture.capture_and_publish`. Проверяются: эпизод и ref каждой книги, соответствие boundary набору ног authoritative snapshot, консьюм dirty только вместе с публикацией, отсутствие филлов без аллокации, суммовой PnL против биржевых филлов.

**A/B:** на `fb06825` сценарий падает на шаге ONE_WAY (сделка `BOTH` не публикуется), на HEAD — **PASS (159 s)**.

**Для ре-ревью.** Импортёр перечитывает последние 60 с перед опубликованным frontier, поэтому книга принимается только после тихого окна (`QUIET_GAP_S` = 65 с): иначе филл снятой книги внутри перекрытия сам триггерит late-fact replay и дефект не воспроизводится. Эта же форма объясняет живую картину 241 `BOTH`-филл / 0 аллокаций внутри уже принятого диапазона. Статус: **закрыто**.

### 2. Mark-price health/fallback считался глобально

**Причина.** Один mark-кадр поднимал общий флаг; после этого REST fallback выключался для всей whitelist, а health оставался `live`, даже если второй символ замолчал навсегда. Тот же дефект в вердикте `check_mark_stream.py`: exit 0, если кадры пришли хотя бы по одному символу.

**Правка** (`b2fea64`). Котировки учитываются посимвольно (`_market_frames`, `_fallback_quotes_at`, `_silent_symbols`); fallback запрашивает только молчащие символы, а не всю whitelist; health публикует `live` только при отсутствии молчащих символов. Гейт: `verdict()` считает покрытие по символам, exit 3 = `PARTIAL`.

**Доказательство.** Unit: `tests/unit/miniapp/test_live_transport.py` (коалесинг, частичный фид, мёртвый сокет), `tests/unit/test_mark_stream_gate.py`. Живьём — новый `tests/testnet/test_live_mark_partial_acceptance.py`, **2 PASS**: testnet-половина (кластер молчит → fallback обязан покрыть котировкой каждый символ whitelist, и только после этого health становится `live`) и mainnet-половина (opt-in `MARK_PARTIAL_LIVE_MAINNET=1`, только публичные unsigned данные: BTCUSDT стримится ~1 кадр/с, ZZZUSDT — никогда; fallback продолжает опрашивать только молчащий символ и больше не трогает стримящийся; `live` не публикуется).

**A/B гейта** на том же partial-фиде: `fb06825` — `VERDICT: STREAMING`, exit 0; HEAD — `VERDICT: PARTIAL`, exit 3. Статус: **закрыто**.

### 3. Private follower stream возвращался в `live` после disconnect

**Причина.** Успешный REST snapshot безусловно возвращал `private_health=True`, а медленный connected-boundary переживает сокет, который его запустил: socket умер → health=false → boundary завершился → health снова true.

**Правка** (`b2fea64`, `miniapp/live.py`). Health выводится из состояния сокета `stream_state["socket_live"]`, а не из факта успешного snapshot: завершившийся boundary мёртвого сокета больше не воскрешает `live`.

**Доказательство.** Живьём — новый `tests/testnet/test_live_stream_health_acceptance.py`: production `MiniAppLiveRuntime` с настоящим `UserStream`, намеренно медленный (8 с) authoritative boundary, обрыв транспорта во время него. **A/B:** на `fb06825` health читается `live` через **7.97 с** после обрыва; на HEAD — **PASS (44.8 s)**.

**Для ре-ревью.** Обрыв идёт через локальный CONNECT-прокси, а не `DELETE /fapi/v3/listenKey`: удаление ключа убивает сокет любого процесса, стримящего тот же аккаунт (включая работающего бота) — первая версия harness'а на этом флакала и мешала живому инстансу. Записано в `tests/AGENTS.md`. Статус: **закрыто**.

### 4. Backoff `UserStream` сбрасывался на handshake

**Причина.** Сброс в base происходил сразу после успешного `async with`, поэтому соединение, умирающее сразу после connect, покупало новый listenKey каждый base-интервал (1→1→1 вместо 1→2→4→…→30), и каждый такой reconnect запускал authoritative follower snapshot.

**Правка** (`b2fea64`, `aster/user_stream.py`). `STABLE_SESSION_S = 30.0`: backoff сбрасывается только если сессия прожила не меньше этого порога.

**Доказательство.** Unit — `tests/unit/aster/test_user_stream_protocol.py` (истекающий сразу после connect сокет). Живьём — лестница измеряется в том же `test_live_stream_health_acceptance.py` на свежем потоке: четыре быстрых обрыва дают рост интервала (base 1 s → ≥ 3 s). Статус: **закрыто**.

### 5. Frontend self-heal backoff фактически всегда оставался около 1 s

**Причина.** Лестница сбрасывалась, когда `snapshot.error === null`, но каждый retry очищал ошибку ещё до ответа, поэтому счётчик обнулялся на каждой попытке.

**Правка** (`44b037b`, `useResource.ts` / `useHistory.ts`). Лестница сбрасывается только когда действительно изменился `receivedAt` (ref `settledAt`), а не потому что in-flight retry очистил `error`.

**Доказательство.** Регрессии на повторные автономные падения в `miniapp/src/useResource.test.tsx` и `historyResources.test.ts` (в дополнение к уже существовавшим activation/foreground и failure→success проверкам). Статус: **закрыто**.

### 6. Скрытые keep-alive экраны оставались live-consumers

**Причина.** Подписка не зависела от `active`, а consumer считался активным просто по `listeners.size`; все четыре tab-root остаются смонтированными, поэтому скрытые Positions/Activity/history участвовали в live-трафике.

**Правка** (`44b037b`). Refcount видимых потребителей: `setActive` / `hasActiveSubscribers` в `resources.ts`, `setActive` / `hasActiveConsumers` в `historyResources.ts`. Live-чтения идут только к активным потребителям; скрытые помечаются stale и обновляются на активации.

**Доказательство.** `miniapp/src/appShellKeepAlive.test.tsx` (live-инвалидация при скрытом подписчике), `historyResources.test.ts`. Подтверждено живьём: `churn` — 50 переключений вкладок и обход вложенных экранов при `liveSockets=1` и 18 запросах. Статус: **закрыто**.

### 7. L4 quote coalescing / render cadence не был реализован

**Причина.** Каждый mark-кадр немедленно инвалидировал UI; при 2 символах × ~1 кадр/с это быстрее измеренного weak-чтения через туннель (~0.9 c), поэтому weak DTO постоянно фенсился новым кадром: PnL замирал, клиент крутил HTTP-loop.

**Правка** (`b2fea64` + `44b037b`). В `miniapp/live.py`: `MARK_RENDER_CADENCE_S = 1.0`, коалесинг по символам (`_pending_marks` / `_flush_mark_prices`), публикация не чаще окна. На фронте: `stateInvalidationRevision` и `Flight.stateInvalidationAtStart` — quote-инвалидация (`source === "mark-price"`) помечает DTO stale, не фенся чтение в полёте; state-публикация фенсит как раньше.

**Доказательство.** Unit: `miniapp/src/resources.test.ts` (state-инвалидация по-прежнему фенсит, quote — нет). Живьём — новый `tests/testnet/test_live_channel_cadence_acceptance.py` против настоящего `/miniapp/api/live` работающего инстанса: hello-baseline, строго возрастающие ревизии, ≤ 2 инвалидации/с при живом mark-фиде, догон «молчащего» читателя ограниченным burst'ом вместо ограждённого бэклога, single-use тикет. **PASS (32 s)**. В реальном UI: `jitter` 70 с — 6 мутаций, все текстовые (PnL и `freshness`), предупреждений нет. Статус: **закрыто**.

## P2

**8. Collision-prone `clientOrderId`.** `manual_master_order.py`: `_order_marker()` = `secrets.token_hex(4)` вместо `int(time.time()) % 100000`; новый `tests/unit/test_manual_master_order_ids.py` (уникальность, формат, граница 36 символов). Закрыто.

**9. Generic manual retry на Account Builder.** Кнопка `Попробовать снова` убрана; упавшее чтение лечит bounded retry, explicit-путь `Перепроверить разрешения` сохранён. Регрессия — `accountBuilderBehavior.test.tsx` (initial load failure без generic retry). Закрыто.

**10. «Один факт — один голос» на nested-экранах.** `railSpeaks` распространён на System Status и Account Builder: тот же transport warning больше не произносится дважды. Закрыто.

**11. Истечение Agent evidence как потеря транспорта.** `evidenceExpired` отделён от `transportStale`: своя копия (`AGENT_EVIDENCE_EXPIRED_NOTE`) и однократный авто-reproof (`reproofedExpiry`) вместо `Данные устарели` + transport-warning. Тест `accountBuilderBehavior.test.tsx` переписан на новую семантику. Закрыто.

**12. a11y: тикающий возраст в `role="status"`.** Вердикт и возраст разведены (`liveStatusVerdict` / `liveStatusAge` в `liveStatus.ts`); динамический возраст вынесен из live-region. Тест — `liveStatus.test.tsx`. Закрыто.

**13. Pull-to-refresh сообщал успех раньше истины.** `pullRefresh()` теперь ожидает `history.revalidateActive()` и бросает `"pull refresh incomplete"`; `allSettled` больше не превращает провал чтений в success-haptic. Тесты — `pullToRefreshBehavior.test.tsx` (провал и завершение истории). Закрыто.

**14. `git diff --check`.** Whitespace-дрейф в `pagingFeedback.test.tsx`, `trustSteps.ts`, `usePaging.ts` снят; `git diff --check main...HEAD` — чисто (проверено на HEAD). Закрыто.

**15. Docs/evidence hygiene.** `scripts/AGENTS.md`: три противоречивые строки про `audit_miniapp_browser.mjs` сведены в одну; Usage самого скрипта дополнен `status/churn/shot` и флагами; дубль `TYPE_CHECKING` в `miniapp/server.py` убран; `DEV_PLAN_SKELETON.md` — §10 в freeze-note вместо §9; 27 POST вместо 26; `check_doc_facts.py` больше не печатает устаревшие suite-числа. Снапшоты прошлых проходов помечены как снапшоты, а не как текущее состояние. Закрыто.

## Гейты на итоговом дереве (HEAD `6d50fe7`)

| Гейт | Результат |
|---|---|
| `pytest -q -m "not testnet"` | **1570 passed / 2 skipped / 92 deselected** (deselected вырос с 87: пять новых live-тестов) |
| `cd miniapp && pnpm test` | **35 files / 412 passed** |
| `cd miniapp && pnpm lint && pnpm build` | PASS |
| `ruff check .` / `ruff format --check .` | PASS (316 файлов) |
| doc links / budgets / `check_doc_facts` | 60 файлов OK / OK / **13 PASS · 1 SKIPPED · 0 FAIL** |
| `git diff --check main...HEAD` | чисто |

## Живая кампания (сверх unit/integration)

Пять новых live-тестов (`CT_TESTNET=1`; последний прогон всех четырёх файлов вместе — **5 passed за 244 s**), реальная смена position mode, реальный продукт и реальный UI:

- реальный master-entry на testnet → копирование на трёх подписчиков с настоящими `STOP_MARKET`, снижение позиции, закрытие; после кампании все пять аккаунтов `flat` / 0 ордеров / исходный HEDGE;
- Chromium/CDP с живой позицией: `jitter` 70 с — 6 текстовых мутаций без предупреждений; `swipe` — полный жест pull-to-refresh (`ready → refreshing → idle`, haptics, `refresh=true` чтения, guard для прокрученного контекста); `series` — четыре вкладки с живыми данными (mark от посимвольного fallback); `churn` — 50 переключений при `liveSockets=1`;
- Telegram Desktop: живая позиция пришла в клиент сама (0 → +4,53 USDT, `1 из 1`, дальше тикала), «Позиции» / «Активность» / «Статус системы» отрисовали реальные данные, pull-to-refresh проиграл жест без слома вёрстки, после закрытия позиции экран сам ушёл в `Открытые 0`, а в «Закрытых» появилась свежая сделка;
- iPhone (проверка владельца): тот же инстанс в Telegram iOS — индикатор pull-to-refresh больше не «полоса», содержимое тянется за пальцем, прокрутка и отрисовка без замечаний.

## Что осталось непроверенным

- **mainnet-trading не запускался** (`CT_ALLOW_MAINNET=false` соблюдён); из mainnet читались только публичные mark-цены во второй половине P1-2.
- **Android-клиент** не проверялся: живой UI — Chromium/CDP, Telegram Desktop и Telegram iOS.
- **Протокольные сбои вне перечисленных сценариев** (деградация биржи, частичный фид на живом проде, сетевые полуобрывы) не воспроизводились.
- Push выполнен: `fb06825..6d50fe7` в `origin/codex/miniapp-live-updates`.

## Просьба к ревьюеру

Перепроверить 15 находок на HEAD `6d50fe7`. Удобные точки входа: таблица файлов в `tests/AGENTS.md` и раздел «live-перепроверка снятых ограничений» в `docs/reports/MINIAPP_LIVE_MODE_PLAN_2026-09.md`. Ожидание: снятие вердикта **MERGE BLOCKED**, либо новая находка с воспроизведением.

