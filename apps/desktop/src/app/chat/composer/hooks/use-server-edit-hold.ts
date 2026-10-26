import { useEffect } from 'react'

import { activeGateway } from '@/store/gateway'

import { holdQueuedPrompt } from '../discard-lost-prompt'

/** The owner's hold lapses after 120 s; an open edit renews it well before that. */
const RENEW_MS = 60_000

/**
 * While a server-queued row is open in the composer, ask its owner to hold it so the row cannot
 * start mid-edit. The hold is renewed while the edit stays open and released when it closes; a
 * saved edit replaces the row, so the release then finds nothing to release.
 */
export function useServerEditHold(
  sessionId: string | null | undefined,
  queueSessionKey: string | null | undefined,
  admissionId: string | null
): void {
  useEffect(() => {
    const gateway = activeGateway()

    if (!gateway || !queueSessionKey || !admissionId) {
      return
    }

    const request = gateway.request.bind(gateway)
    // A refused hold (the row already started) leaves the edit to the save path's own refusal.
    const hold = (held: boolean) => void holdQueuedPrompt(sessionId, queueSessionKey, admissionId, held, request).catch(() => undefined)

    hold(true)
    const timer = window.setInterval(() => hold(true), RENEW_MS)

    return () => {
      window.clearInterval(timer)
      hold(false)
    }
  }, [admissionId, queueSessionKey, sessionId])
}
