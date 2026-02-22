/**
 * IndexedDB wrapper for persistent 20m Signal Decoder storage.
 *
 * DB: sdr-monitor  version: 1
 * Stores:
 *   cw-sessions  — CW transmission sessions (max 500)
 *   sstv-frames  — Auto-decoded SSTV frames   (max 100)
 */

const DB_NAME = 'sdr-monitor';
const DB_VERSION = 1;

export interface CWSession {
  id?: number; // autoIncrement key
  startTs: string; // ISO timestamp of first character
  endTs: string; // ISO timestamp of last character
  text: string; // decoded text
  freqHz: number; // RF frequency in Hz
}

export interface SSTVFrame {
  id?: number; // autoIncrement key
  ts: string; // ISO timestamp of detection
  imageUrl: string; // data: URL (PNG)
  mode: string; // SSTV mode name
}

// ── Internal DB open ──────────────────────────────────────────────────────────

let _db: IDBDatabase | null = null;

function openDB(): Promise<IDBDatabase> {
  if (_db) return Promise.resolve(_db);
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains('cw-sessions')) {
        db.createObjectStore('cw-sessions', { keyPath: 'id', autoIncrement: true });
      }
      if (!db.objectStoreNames.contains('sstv-frames')) {
        db.createObjectStore('sstv-frames', { keyPath: 'id', autoIncrement: true });
      }
    };
    req.onsuccess = () => {
      _db = req.result;
      resolve(_db);
    };
    req.onerror = () => reject(req.error);
  });
}

// ── Generic helpers ───────────────────────────────────────────────────────────

function put<T>(storeName: string, record: T): Promise<void> {
  return openDB().then(
    (db) =>
      new Promise((resolve, reject) => {
        const tx = db.transaction(storeName, 'readwrite');
        const st = tx.objectStore(storeName);
        st.add(record);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      })
  );
}

function getAll<T>(storeName: string): Promise<T[]> {
  return openDB().then(
    (db) =>
      new Promise((resolve, reject) => {
        const tx = db.transaction(storeName, 'readonly');
        const st = tx.objectStore(storeName);
        const req = st.getAll();
        req.onsuccess = () => resolve(req.result as T[]);
        req.onerror = () => reject(req.error);
      })
  );
}

/** Delete oldest records until count ≤ max. */
function purgeOldest(storeName: string, max: number): Promise<void> {
  return openDB().then(
    (db) =>
      new Promise((resolve, reject) => {
        const tx = db.transaction(storeName, 'readwrite');
        const st = tx.objectStore(storeName);
        const cReq = st.count();
        cReq.onsuccess = () => {
          const excess = cReq.result - max;
          if (excess <= 0) {
            resolve();
            return;
          }
          // Open a cursor to delete the oldest (lowest key) records
          let deleted = 0;
          const cursor = st.openCursor();
          cursor.onsuccess = (e) => {
            const c = (e.target as IDBRequest<IDBCursorWithValue | null>).result;
            if (!c || deleted >= excess) {
              resolve();
              return;
            }
            c.delete();
            deleted++;
            c.continue();
          };
          cursor.onerror = () => reject(cursor.error);
        };
        cReq.onerror = () => reject(cReq.error);
      })
  );
}

// ── Public API ────────────────────────────────────────────────────────────────

const MAX_CW_SESSIONS = 500;
const MAX_SSTV_FRAMES = 100;

export async function saveCWSession(session: CWSession): Promise<void> {
  await put('cw-sessions', session);
  await purgeOldest('cw-sessions', MAX_CW_SESSIONS);
}

export function listCWSessions(): Promise<CWSession[]> {
  return getAll<CWSession>('cw-sessions');
}

export async function saveSSTV(frame: SSTVFrame): Promise<void> {
  await put('sstv-frames', frame);
  await purgeOldest('sstv-frames', MAX_SSTV_FRAMES);
}

export function listSSTV(): Promise<SSTVFrame[]> {
  return getAll<SSTVFrame>('sstv-frames');
}
