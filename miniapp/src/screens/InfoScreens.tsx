/** Short static Help and Security screens (эталон 06-settings, P2.3).
 *
 * Pure explainer content: they describe how the service works and its
 * permission model, and never claim the live per-account state — actual
 * Agent permissions/expiry belong to the Account+Builder attestation screen
 * (a later part), so nothing here pretends to prove them. */

import { Brand } from '../components/ui'

export function HelpScreen({ accountMasked }: { accountMasked?: string | null }) {
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

      <div className="info-box">
        Подключение, отключение и экстренные действия выполняются в боте —
        мини-ап показывает состояние и безопасные настройки.
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
        <h2 className="h2">Ключи остаются у вас</h2>
        <p className="lede" style={{ margin: 0 }}>
          Приватный ключ основного кошелька не вводится в сервис и не хранится
          в нём. Подключение работает через отдельный Agent-ключ, который
          выдаётся подписью авторизации.
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
