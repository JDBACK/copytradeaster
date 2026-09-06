# Исправления движка и план повторной квалификации

Дата: 2026-09-05. Ветка: `codex/live-copy-tests-hardening`.
Исходное ревью: коммит `2c89286`. Исправления находятся в текущем рабочем дереве.

**Реализация движка и корректирующих live-тестов готова. Следующему исполнителю
осталось проводить тесты, разбирать отклонения и исправлять код или сценарии
по полученным доказательствам. Нового разрешения на mainnet этот документ не даёт.**

**Вердикт ревью: прежний GO недостаточно обоснован; до повторной квалификации —
NO-GO для mainnet.** Старые A–G сохраняются как история прогонов предыдущего кода.
Зелёная старая матрица не доказывала новые инварианты между компонентами.
Архитектура сохранена: reconciliation, SQLite journal, Decimal, отдельный outbox,
один процесс и явные границы модулей.

Актуальные счётчики и coverage находятся только в
[ENGINE_TEST_STATUS_2026-08.md](../ENGINE_TEST_STATUS_2026-08.md).
Журнал кампании —
[PRE_MAINNET_HARDENING_PROGRESS_2026-09.md](PRE_MAINNET_HARDENING_PROGRESS_2026-09.md).

## Что исправлено

| Ревью | Проблема предыдущего кода | Реализованное изменение |
|---|---|---|
| R1 | Немаркированный load-тест попадал в `-m "not testnet"` | Общий collection guard для всего `tests/testnet`, явные opt-in для load/worker; файловая блокировка параллельных signed pytest-кампаний |
| R2 | Cleanup load выполнялся только в успешной ветке | Начальный flat-proof до запуска движка; cleanup и независимый signed audit в `finally`, включая ошибку boot и выбор отдельных фаз |
| R3 | NetworkProfile не доходил до реальных credentials/adapters; preflight не вызывался из App | Eager validation Settings, chain из профиля в credentials и onboarding; production mainnet boot вызывает preflight до торговых действий и отказывает при непроверенном режиме |
| R4 | Новый quantity/revision обходил UNKNOWN старой команды | Восстановление перед новым снимком; Executor блокирует новый риск аккаунта при незавершённой экспозиции; другой аккаунт продолжает работу. Частичный position delta без terminal history остаётся UNKNOWN |
| R5 | Две HEDGE-настройки с одинаковым action_id освобождали неверную зависимость | Один консервативный SET на символ, общая зависимость обоих входов; дубли action_id отклоняются; защита существующей позиции не зависит от успеха SET |
| R6 | Worker повышал плечо после stop-driven downshift; старый CONFIRMED SET подменял текущую проверку | Плечо ниже ceiling считается готовым; worker сначала читает конфигурацию и сохраняет меньшее значение. Snapshot, execution и maintenance используют общий замок аккаунта. Старый SET проверяется по текущему positionRisk |
| R7 | Проверка относительно entry отбрасывала допустимую liquidation в прибыльной позиции | Направление liquidation определяется относительно mark; общий stop-risk HEDGE-ног учитывается без взаимозачёта |
| R8 | Гонка регистрации превышала launch cap; boot обходил cap | Проверка REAL-cap внутри атомарной публикации, в том числе SHADOW→REAL. Boot сохраняет все аккаунты для защиты/закрытия, но запрещает увеличения при превышении cap |
| R9 | Разность размеров bounded journal давала ложные нули; master READY стирал degraded | Recompute возвращает число оценённых действий, включая повторы/блокировки. Scope снимка локален вызову. Health требует полного authoritative отчёта, нулевой сходимости, отсутствия in-flight и готового плеча; исторический FAILED сам по себе не удерживает degraded |

Дополнительно введён общий бюджет аккаунта: `CT_MAX_TOTAL_RISK_PCT=0.10` от
USDT equity и общий резерв не более 80% available margin на все увеличения.
Существующие позиции расходуют бюджет; неподтверждённые закрытия и ужесточения
стопов его не освобождают. Без наблюдаемого стопа новый риск не добавляется.
`CT_OPERATOR_MAX_LEVERAGE` — необязательный ceiling, общий для Coordinator и worker.
Это бюджет убытка до стопа, а не гарантия исполнения при гэпе и не точная модель
биржевой cross-margin liquidation.

## Готовые проверки

- [x] Исправления production-кода и новые офлайн-регрессии:
  [test_corrective_invariants.py](../../tests/unit/trading/test_corrective_invariants.py),
  [test_corrective_boot.py](../../tests/unit/test_corrective_boot.py).
- [x] Обновлены прежние unit-контракты: lower leverage readiness,
  partial-progress UNKNOWN, реальные изменения состояния вместо journal-count proof.
- [x] Реализован новый signed пакет
  [test_engine_corrective_live.py](../../tests/testnet/test_engine_corrective_live.py).
- [x] Обновлены UNKNOWN, network, risk и load live-сценарии под новый контракт.
- [x] Проверены lint/format, offline suite, collection и отказ live-тестов без opt-in.
- [x] Выполнены signed прогоны исправленного дерева (2026-09-06, REAL Testnet):
  UNKNOWN 6/6 + network 3/3 + LC1–LC6 6/6 + совместимость 20/20 + load 0.3
  (30/30 циклов, restart/SIGTERM/SIGKILL, метрики, FLAT-ALL). A4 → ETHUSDT
  (BTC-окна saturated), A5 delta-count, load scope 100→30 решением owner'а.
- [x] Разобраны все failures/skips, вердикт обновлён по доказательствам
  (TL;DR в ENGINE_TEST_STATUS + запись 2026-09-06 в PROGRESS-журнале):
  3 прод-бага пофикшены вживую, 3 honest shape-skip зафиксированы открытыми,
  -4226 задокументирован, вердикт COMPLETE (owner scope), mainnet не разрешён.

| ID | Готовый тест / проверка | Что должно быть доказано запуском |
|---|---|---|
| A2+ | `test_open_all_recovery_reads_down_holds_unknown_without_retry` в UNKNOWN-файле | После настоящего принятого OPEN и потери reads новая qty/revision блокируется; восстановление через настоящую history не создаёт ещё один OPEN |
| A3 | `test_partial_close_response_lost_confirms_by_real_position_delta` | Полностью исполненный ордер, закрывающий часть позиции, подтверждается полным ожидаемым delta. Это не доказательство частичного исполнения одного ордера |
| A6 | `test_partial_position_progress_with_hidden_history_stays_unknown` | Настоящие OPEN и конкурентное уменьшение оставляют неполный delta; UNKNOWN удерживается до history, повторный вход не отправляется. Сценарий прямо обозначен как два full-fill, не как искусственный partial fill |
| LC1 | `test_shared_hedge_leverage_rejection_retry_and_worker_restart` | Асимметричные реальные HEDGE-ноги, один общий SET; injected rejection блокирует оба входа; повтор FAILED не считается нулём; восстановление, maintenance и restart сохраняют меньшее плечо |
| LC2 | `test_account_budget_on_two_real_symbols[risk]` | Сумма рисков BTC/ETH ограничена общим бюджетом; обе реальные позиции соответствуют запланированным qty и защищены |
| LC3 | `test_account_budget_on_two_real_symbols[margin]` | Два по отдельности допустимых входа не расходуют один available balance дважды; суммарная margin ограничена до отправки |
| LC4 | `test_shared_liquidation_quote_uses_mark_geometry` | Наблюдаемая signed HEDGE geometry проверяется относительно mark с отдельным oracle; нулевая liquidation честно означает отсутствие live-доказательства этой геометрии |
| LC5 | `test_real_registration_cap_is_atomic_and_boot_keeps_close_only_accounts` | Конкурентная регистрация последнего REAL-слота, boot с превышением cap, сохранение уже открытой позиции и возможность закрытия без новых входов |
| LC6 | `test_full_reconcile_health_after_saturated_journal` | Исторический journal больше окна recent не скрывает действия; только authoritative follower convergence снимает degraded; master READY и scoped sweep недостаточны |
| N1 | `test_mainnet_profile_app_composition_no_orders` | Проверяется chain реальной загруженной TradingIdentity, а не только `app.cfg`; mainnet App не запускается и mainnet requests не отправляются |
| D / liquidation | `test_liquidation_invariant_holds_safe_leg_live` | Signed safe-leg hold с правильным mark-oracle; удалено неверное доказательство «нарушение cross-margin невозможно» |
| F | `test_hybrid_load_real_plus_shadow` | Production-equivalent load после изменений; cleanup всех фаз и boot failures; master UNKNOWN использует production resolver без слепого residual retry |

LC1/LC4 могут вернуть skip, если реальные баланс/конфигурация/котировка не дали
требуемой геометрии. Такой skip **не закрывает** соответствующий acceptance-пункт.
Следующий исполнитель фиксирует signed evidence и корректирует допустимый
testnet shaping, либо оставляет конкретное ограничение открытым. Нельзя
подменять геометрию фальшивым `positionRisk` и называть это live-квалификацией.

## Порядок запуска

Все команды выполнять из корня репозитория. Сначала зафиксировать текущий diff
и идентификатор проверяемого дерева. Не сбрасывать изменения других задач.
Все signed сценарии запускать последовательно на выделенных testnet-аккаунтах;
pytest lock не защищает от отдельно запущенного бота или ручного скрипта.
Проверить отсутствие другого торгового процесса, исходный flat/zero-orders,
наличие `wallet.txt` и provisioned encrypted DB. Главный DB используется только
как источник credentials; тесты пишут в scratch. Секреты не печатать.

1. Повторить быстрый gate на дереве исполнителя:

   ```bash
   CT_TESTNET=0 CT_LOAD=0 .venv/bin/pytest -q -m "not testnet" --cov=src/copytradeaster
   .venv/bin/ruff check .
   .venv/bin/ruff format --check .
   ```

   Coverage сверить с каноническим статусом и документарной целью 87%.
   Текущий замер ниже этой цели: не объявлять coverage gate пройденным.
   Если дорабатываются найденные failure paths, включить их регрессии в повторный замер.

2. Запустить целевую corrective-квалификацию, сохранив артефакты:

   ```bash
   CT_TESTNET=1 CT_POSITION_MODE=HEDGE .venv/bin/pytest -v \
     tests/testnet/test_pre_mainnet_unknown_live.py \
     tests/testnet/test_pre_mainnet_network_live.py \
     tests/testnet/test_engine_corrective_live.py \
     --basetemp=data/corrective-live-run --junitxml=data/corrective-live-junit.xml
   ```

   `--basetemp` очищается pytest при повторном запуске: перед повтором архивировать
   предыдущую папку или выбрать новое имя. Новый пакет сохраняет `evidence.json`
   со снимками, вычисленными резервами и двумя финальными signed flat-proof.
   В нём App загружается штатно; автоматические follower-generations и maintenance
   на время контролируемого сценария останавливаются, master WS и настоящие REST
   остаются. Injection отказа SET помечен явно. LC6 содержит синтетическую
   **историческую запись журнала**, реальные fills/позиции не подменяются.

3. Проверить совместимость существующих сценариев с новым движком:

   ```bash
   CT_TESTNET=1 CT_POSITION_MODE=HEDGE .venv/bin/pytest -v \
     tests/testnet/test_pre_mainnet_master_live.py \
     tests/testnet/test_pre_mainnet_risk_live.py \
     tests/testnet/test_pre_mainnet_isolation_live.py \
     tests/testnet/test_pre_mainnet_limit_live.py \
     tests/testnet/test_pre_mainnet_hedge_live.py
   ```

   LIMIT L2 закрывается только наблюдаемым real partial fill. Старый honest skip
   не переименовывать в успех. Отдельно прогнать прежние builder corrective и
   базовый ONE_WAY E2E; сохранить существующий scope этих файлов.

4. Только после разбора предыдущих результатов — полный load без сокращения:

   ```bash
   CT_TESTNET=1 CT_LOAD=1 CT_POSITION_MODE=HEDGE CT_LOAD_SCALE=1.0 CT_LOAD_PHASES=all \
     .venv/bin/pytest -v tests/testnet/test_pre_mainnet_load_live.py \
     --basetemp=data/corrective-load-run --junitxml=data/corrective-load-junit.xml
   ```

   `CT_LOAD=1` для offline shadow-scale и signed load — разные сценарии;
   один не заменяет другой. Не запускать несколько кампаний на общих аккаунтах.

## При сбое и критерий завершения

Сохранить nodeid, точную команду, tree/commit, исключение, scratch DB, log,
command_key/COID/status/attempts, relevant signed history/positionRisk/openOrders,
а для риска — исходный balance, mark, stop, qty и leverage. Разделить ошибку
движка, дефект сценария и недоступность Testnet. Сначала обеспечить cleanup и
подтвердить flat; затем воспроизвести и исправить причину, добавить или уточнить
регрессию и повторить затронутый gate. Не снижать assertions ради зелёного отчёта.

Завершение квалификации: реальные сценарии подтверждены на исправленном дереве,
нет необъяснённых failures/UNKNOWN, у skips явно указан незакрытый пункт,
экономические повторы отсутствуют, final signed audit чист, load не сокращён.
Обновить канонический статус, этот чеклист, roadmap и append-only журнал.
После этого сформулировать новый GO/NO-GO с ограничениями; наличие готового
кода тестов само по себе GO не даёт.

## Отклонение от критерия (решение owner'а, 2026-09-06)
Критерий «load не сокращён» нарушен явно: load scope 100→30
(`CT_LOAD_SCALE=0.3`). Основание: «100» — эмоциональная цифра без инженерного
обоснования; механика доказывается за 5–10 циклов, фолт-фазы от глубины не
зависят, хвосты ловят только грозы биржи. Полный load 1.0 остаётся открытым
пунктом (не проваленным — отложенным). P8a-B и moot-by-flat — тоже решения
owner'а (см. TL;DR и PROGRESS-журнал). Вердикт: COMPLETE (owner scope).

## Контаминация live-окон фронтом (2026-09-07, STOP)
Фронт-стенд (`copytradeaster-front/data/miniapp-preview.sqlite3`, 2301
команда с 2026-09-02) торгует теми же main/copyc1-3/tg-8264892202. Все
ghost-fills найдены у него как CONFIRMED. Чистые: Step-2, compat,
fix-reruns, A5-rerun (0 строк фронта в окне), весь офлайн.
Контаминированы: load5/6/7 (1900+ команд фронта в окнах; -4226-шторм —
кандидат в интерференцию двух движков: все 27 FAILED только на REAL,
ноль на shadow), L1-rerun (даблы 50–200мс доказаны fills), F3/F3b
(ghost-open при идеальном denial: 1 FAILED SET + BLOCKED + 0 sends).
Движок F3 полностью оправдан; L1-green отозван как слепой к удвоению.
Дальнейшие live-прогоны — только после решения owner'а (стоп стенда /
развод ключей): чистые rerun F3, L1, load. Mainnet NO-GO.
