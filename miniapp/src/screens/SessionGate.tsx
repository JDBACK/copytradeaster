/** Access gate: server-derived read-only states before any wallet flow. */

import { closeMiniApp } from '../telegram'
import { Brand } from '../components/ui'

export type GateState = 'loading' | 'pending' | 'rejected' | 'invalid'

export function SessionGate({ state }: { state: GateState }) {
  if (state === 'loading') {
    return (
      <div className="screen">
        <Brand />
        <div className="skeleton" style={{ minHeight: 220 }} aria-label="Загрузка" />
        <div className="skeleton" style={{ minHeight: 90 }} />
        <div className="footer-note">Проверяем доступ…</div>
      </div>
    )
  }

  if (state === 'invalid') {
    return (
      <div className="screen">
        <Brand />
        <h1 className="h1">Откройте из бота</h1>
        <p className="lede">
          Сессия недействительна или устарела. Закройте это окно и запустите мини-ап кнопкой в чате с
          ботом.
        </p>
        <div className="btn-stack">
          <button type="button" className="btn btn-primary" onClick={closeMiniApp}>
            Вернуться в бота
          </button>
        </div>
      </div>
    )
  }

  const pending = state === 'pending'
  return (
    <div className="screen">
      <Brand right={<span className="badge-testnet">Aster testnet</span>} />
      <h1 className="h1">{pending ? 'Заявка на проверке' : 'Доступ отклонён'}</h1>
      <p className="lede">
        {pending
          ? 'Заявка на доступ уже у администратора. Отправлять повторно не нужно — вернитесь в мини-ап после одобрения.'
          : 'Заявка на доступ отклонена администратором. Причина — в чате с ботом.'}
      </p>
      <div className="info-box">
        Управление подключением и заявками доступно в боте.
      </div>
      <div className="btn-stack">
        <button type="button" className="btn btn-secondary" onClick={closeMiniApp}>
          Вернуться в бота
        </button>
      </div>
    </div>
  )
}
