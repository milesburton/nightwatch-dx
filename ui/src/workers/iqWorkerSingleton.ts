/**
 * IQ Worker singleton — shared across all panels (CWLogPanel, WaterfallPanel,
 * SSTVGalleryPanel) so only one WebSocket connection is made to rtl-bridge.
 *
 * Import { addIQListener } from this module instead of from CWPanel.
 */

import type { IQWorkerMessage } from '../types.js';

let _worker: Worker | null = null;
const _listeners = new Set<(msg: IQWorkerMessage) => void>();

function getIQWorker(): Worker {
  if (!_worker) {
    _worker = new Worker(new URL('./iqWorker.ts', import.meta.url), { type: 'module' });
    _worker.onmessage = (e: MessageEvent<IQWorkerMessage>) => {
      for (const fn of _listeners) fn(e.data);
    };
  }
  return _worker;
}

/** Subscribe to IQ worker messages. Returns an unsubscribe function. */
export function addIQListener(fn: (msg: IQWorkerMessage) => void): () => void {
  getIQWorker(); // ensure worker is started
  _listeners.add(fn);
  return () => _listeners.delete(fn);
}
