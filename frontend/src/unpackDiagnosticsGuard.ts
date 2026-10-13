/** Single-dialog request identity fence for read-only manual reconciliation.
 *
 * A remote response must never be shown under a different movie, after a
 * close/reopen, or after a newer request to the same movie has superseded it.
 * This is intentionally separate from the persisted journal/REST API.
 */
export interface UnpackDiagnosticsTicket {
  readonly itemId: string;
  readonly epoch: number;
}

export function createUnpackDiagnosticsGuard() {
  let activeItemId: string | null = null;
  let epoch = 0;

  return {
    open(itemId: string): UnpackDiagnosticsTicket {
      activeItemId = itemId;
      epoch += 1;
      return { itemId, epoch };
    },
    capture(itemId: string): UnpackDiagnosticsTicket | null {
      return activeItemId === itemId ? { itemId, epoch } : null;
    },
    isCurrent(ticket: UnpackDiagnosticsTicket): boolean {
      return activeItemId === ticket.itemId && epoch === ticket.epoch;
    },
    close(): void {
      activeItemId = null;
      epoch += 1;
    },
  };
}
