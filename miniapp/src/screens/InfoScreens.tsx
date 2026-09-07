/** Short static Help and Security screens (эталон 06-settings, P2.3).
 *
 * Pure explainer content: they describe how the service works and its
 * permission model, and never claim the live per-account state — actual
 * Agent permissions/expiry belong to the Account+Builder attestation screen
 * (a later part), so nothing here pretends to prove them. */

import type { NestedScreen } from '../navigation'
import { Brand } from '../components/ui'

export function HelpScreen({ accountMasked, onOpen }: { accountMasked?: string | null; onOpen?: (route: NestedScreen) => void }) {
  return (
    <div className="screen">
      <Brand
        right={
          accountMasked ? (
            <span className="wallet-chip num">{accountMasked}</span>
          ) : (
            <span className="badge-testnet">Aster testnet</span>
          )
        }
      />
      <h1 className="h1" style={{ marginTop: 4 }}>
        Помощь
      </h1>

      <section className="panel">
        <h2 className="h2">Как работает копирование</h2>
        <p className="lede" style={{ margin: 0 }}>
          Система повторяет целевое состояние позиций мастера на вашем счёте:
          размер позиции рассчитывается от вашего баланса и риска на
          защитный Stop, плечо подбирается автоматически.
        </p>
      </section>

      <section className="panel">
        <h2 className="h2">Пауза и возобновление</h2>
        <p className="lede" style={{ margin: 0 }}>
          Пауза останавливает только новые входы. Защитные Stop/TP, сокращения
          и закрытия продолжают работать. Возобновление требует отдельного
          подтверждения.
        </p>
      </section>

      <section className="panel">
        <h2 className="h2">Уведомления</h2>
        <p className="lede" style={{ margin: 0 }}>
          События копирования приходят в чат с ботом. Включить или выключить
          доставку можно в разделе «Уведомления»; это не влияет на торговлю.
        </p>
      </section>

      <section className="panel"><h2 className="h2">Почему отличаются объёмы?</h2><p className="lede">Размер зависит от вашей доступной суммы, риска на стоп и ограничений инструмента. Он не обязан совпадать с объёмом мастера.</p><button type="button" className="btn btn-ghost" onClick={() => onOpen?.({ kind: 'risk' })}>Настроить риск</button></section>
      <section className="panel"><h2 className="h2">Почему PnL сейчас отличается от итога?</h2><p className="lede">PnL открытой позиции меняется по маркировочной цене. Итог закрытой сделки учитывает подтверждённые исполнения и начисления. Пока комиссии и funding не сверены, показан PnL до комиссий.</p><button type="button" className="btn btn-ghost" onClick={() => onOpen?.({ kind: 'trade-list', state: 'closed', period: 'all' })}>Открыть историю сделок</button></section>
      <section className="panel"><h2 className="h2">Почему не открылась сделка?</h2><p className="lede">Вход может быть остановлен паузой, недостатком доступной суммы, отсутствием стопа или неподтверждённым подключением. Причина видна в активности и статусе.</p><button type="button" className="btn btn-ghost" onClick={() => onOpen?.({ kind: 'system-status' })}>Проверить подключение</button></section>
      <button type="button" className="btn btn-secondary" onClick={() => onOpen?.({ kind: 'account-builder' })}>Как отключить аккаунт</button>
      <div className="info-box">
        Экстренные действия доступны администратору. Отключение аккаунта в сервисе не закрывает позиции и автоматически не отзывает разрешения Agent на Aster.
      </div>
    </div>
  )
}

export function SecurityScreen({ accountMasked }: { accountMasked?: string | null }) {
  return (
    <div className="screen">
      <Brand
        right={
          accountMasked ? (
            <span className="wallet-chip num">{accountMasked}</span>
          ) : (
            <span className="badge-testnet">Aster testnet</span>
          )
        }
      />
      <h1 className="h1" style={{ marginTop: 4 }}>
        Безопасность
      </h1>

      <section className="panel">
        <h2 className="h2">Ключ основного кошелька остаётся у вас</h2>
        <p className="lede" style={{ margin: 0 }}>
          Приватный ключ основного кошелька не вводится в сервис и не хранится
          в нём. Подключение работает через отдельный Agent-ключ, который
          выдаётся подписью авторизации. Сервис хранит отдельный Agent-ключ зашифрованно.
        </p>
      </section>

      <section className="panel">
        <h2 className="h2">Модель разрешений</h2>
        <p className="lede" style={{ margin: 0 }}>
          По модели разрешений запрашивается только торговля фьючерсами; вывод
          средств не запрашивается. Комиссия Builder ограничена утверждённым
          потолком при подключении.
        </p>
      </section>

      <div className="info-box">
        Разрешения и срок действия Agent проверяются сервером; в мини-апе
        отображаются только проверенные факты без секретов.
      </div>
    </div>
  )
}
