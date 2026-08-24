# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- Фолловер: подключает выделенный Aster V3 account, задаёт риск, включает или
  приостанавливает копирование, контролирует состояние и получает уведомления.
- Оператор/master trader: следит за общим runtime, управляет глобальной паузой,
  reconciliation и аварийным закрытием.

Первый рабочий масштаб — 10–30 followers, максимум около 100.

## Product Purpose

Copytradeaster копирует целевое состояние позиций одного master trader на
followers через проверенное Aster futures trading core. Stage 2 делает это ядро
доступным через понятный Telegram-интерфейс: direct V3 onboarding, настройки
риска, управление, status и уведомления.

Успех для пользователя — подключить testnet account без знания внутренней
архитектуры, сразу понимать, готово ли копирование и какой риск установлен, а
затем безопасно pause/resume или выполнить подтверждённое аварийное действие.

## Positioning

Продукт копирует состояние, а не буквально команды master. WebSocket служит
быстрым сигналом, а authoritative REST snapshots, durable execution journal и
reconciliation восстанавливают правильное состояние после повторов, ошибок и
рестартов.

## Operating Context

- текущий интерфейс Stage 2 — private Telegram chat;
- основной способ управления — inline screens и buttons под сообщением;
- slash-команды остаются только входом, recovery и shortcut-механизмом;
- будущий Telegram Mini App предоставляет те же пользовательские возможности
  через общий application layer, а не отдельную бизнес-реализацию;
- Stage 2 работает только с Aster futures testnet;
- один Python/asyncio process и SQLite WAL обслуживают bot и trading core.

## Capabilities and Constraints

- один master account;
- direct V3 credentials: user address, signer address, signer private key и
  проверка Agent permissions;
- private key хранится только зашифрованно, master wallet key не принимается;
- risk policy: процент риска, leverage cap и entry deviation;
- per-account pause/resume, status, notifications и подтверждённые emergency
  actions;
- Telegram handlers не вызывают trading endpoints напрямую;
- trading core Stage 1 не переписывается;
- mainnet, Builder, WebApp wallet signatures и monetization относятся к более
  позднему этапу;
- direct private-key input через Telegram является testnet-only и не считается
  production-secure onboarding.

## Brand Commitments

- рабочее имя проекта: copytradeaster / AsterDex copy-trade bot;
- язык текущего интерфейса — русский;
- голос интерфейса — прямой, спокойный и точный: без маркетингового шума,
  жаргона и пугающих технических дампов;
- меню должно быть современным, компактным и объяснять последствия действий до
  подтверждения.

## Evidence on Hand

- принятое торговое ядро и актуальные тестовые доказательства:
  `docs/ENGINE_TEST_STATUS_2026-08.md`;
- архитектурные инварианты: `docs/ARCHITECTURE.md`;
- Stage 1 corrective acceptance:
  `docs/reports/STAGE1_CORRECTIVE_REFACTOR_PLAN_2026-08.md`;
- актуальное исследование aiogram:
  `docs/reports/STAGE2_AIOGRAM_RESEARCH_2026-08.md`.

Готового логотипа, визуальной системы или утверждённого дизайна Mini App в
репозитории нет; будущая работа не должна их выдумывать как уже существующие.

## Product Principles

1. Сначала показать состояние и следующий безопасный шаг, затем действие.
2. Одна бизнес-операция и один набор правил для Telegram и Mini App.
3. Опасные действия всегда объясняют scope и требуют отдельного подтверждения.
4. Потеря Telegram UI/уведомления не меняет результат торговой операции.
5. Сложность инфраструктуры появляется только после измеримой необходимости.
