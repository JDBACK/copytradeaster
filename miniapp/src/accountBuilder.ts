/** Shared Account + Builder attestation view-model (P2.6, эталон 07-account-builder).
 *
 * One authoritative Builder verdict for the Account screen, Home and System
 * Status: ``done === true`` only for a backend ``verified`` state, read from
 * the server-derived ``builder_verified`` flag.  Snapshot presence,
 * ``builder_name`` or ``max_fee_rate`` never imply verification — the
 * frontend performs no Builder policy of its own. */

import type { AccountBuilderDTO, AccountBuilderReason, AccountBuilderState } from './types'

/** The single shared Builder verdict: true only for authoritative verified. */
export function isBuilderVerified(dto: { builder_verified?: boolean }): boolean {
  return dto.builder_verified === true
}

/** Same verdict over the Account screen DTO (single truth, no local policy). */
export function isAccountBuilderVerified(dto: AccountBuilderDTO): boolean {
  return dto.state === 'verified'
}

export type AccountBuilderTone = 'success' | 'warning' | 'danger' | 'muted'

export function stateCopy(state: AccountBuilderState): { label: string; tone: AccountBuilderTone } {
  switch (state) {
    case 'verified':
      return { label: 'Builder подтверждён', tone: 'success' }
    case 'expiring':
      return { label: 'Требуется переавторизация', tone: 'warning' }
    case 'stale':
      return { label: 'Данные устарели', tone: 'warning' }
    case 'blocked':
      return { label: 'Требуется переавторизация', tone: 'danger' }
    case 'unavailable':
      return { label: 'Проверка недоступна', tone: 'muted' }
    case 'not_applicable':
      return { label: 'Прямое подключение', tone: 'muted' }
    default:
      return { label: 'Неизвестно', tone: 'muted' }
  }
}

/** Backend-controlled reason rendered as a safe one-line explanation. */
export function reasonCopy(reason: AccountBuilderReason): string {
  switch (reason) {
    case 'none':
      return 'Agent и Builder подтверждены подписями Aster.'
    case 'direct_account':
      return 'Аккаунт подключён напрямую, без Builder. Это нормальное состояние.'
    case 'account_disconnected':
      return 'Локальные данные отключены. Подключите аккаунт заново.'
    case 'agent_invalid':
      return 'Agent отклонён проверкой Aster. Требуется переавторизация.'
    case 'agent_expiring':
      return 'Срок Agent подходит к концу. Обновите авторизацию заранее.'
    case 'builder_not_confirmed':
      return 'Aster не подтвердил Builder для этого аккаунта.'
    case 'builder_mismatch':
      return 'На Aster зарегистрирован другой Builder. Локальное подтверждение сброшено.'
    case 'fee_mismatch':
      return 'Комиссия на Aster отличается от ожидаемой. Локальное подтверждение сброшено.'
    case 'transient_unavailable':
      return 'Aster временно недоступен. Показаны последние подтверждённые данные.'
    case 'snapshot_missing':
      return 'Подтверждённых данных пока нет. Запустите перепроверку.'
    case 'identity_changed':
      return 'Аккаунт изменился во время проверки. Повторите действие.'
    default:
      return 'Состояние неизвестно. Запустите перепроверку.'
  }
}

/** Local disconnect confirmation copy: explicit that Aster-side
 * Agent/Builder authorization is NOT revoked by this action. */
export const DISCONNECT_CONFIRM_TITLE = 'Отключить аккаунт?'
export const DISCONNECT_CONFIRM_BODY =
  'Сервис сначала поставит аккаунт на паузу. Отключение разрешено только без открытых ' +
  'позиций, ордеров и выполняющихся команд. Локальные данные и привязка будут удалены. ' +
  'Авторизация Agent и Builder на стороне Aster этим действием не отзывается.'
