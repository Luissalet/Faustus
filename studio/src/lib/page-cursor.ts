/** A page is admitted only by the request that still owns this feed. */
export interface PageTicket { offset: number; signal: AbortSignal; }

export class PageCursor<T extends { id: string }> {
  rows: T[] = [];
  offset = 0;
  private active: PageTicket | null = null;
  private controller: AbortController | null = null;

  begin(append: boolean): PageTicket | null {
    if (append && this.active) return null;
    this.cancel();
    if (!append) { this.rows = []; this.offset = 0; }
    this.controller = new AbortController();
    this.active = { offset: this.offset, signal: this.controller.signal };
    return this.active;
  }

  accept(ticket: PageTicket, rows: T[]): T[] | null {
    if (this.active !== ticket || ticket.signal.aborted) return null;
    this.offset = ticket.offset + rows.length;
    this.rows = [...new Map([...this.rows, ...rows].map(row => [row.id, row])).values()];
    this.active = null;
    return this.rows;
  }

  fail(ticket: PageTicket): boolean {
    if (this.active !== ticket || ticket.signal.aborted) return false;
    this.active = null;
    return true;
  }

  cancel(): void {
    this.controller?.abort();
    this.controller = null;
    this.active = null;
  }
}
