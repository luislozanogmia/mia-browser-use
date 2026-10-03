/**
 * The reel: screenshots of the pages the human looked at and the answers bots
 * gave there, in the order they happened. Kept in this browser only
 * (IndexedDB of the extension), shared by the background and reel.html.
 */
const REEL_DB = "ghost-reel";
const REEL_STORE = "entries";
const REEL_MAX = 500;

function reelDb() {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open(REEL_DB, 1);
    open.onupgradeneeded = () => open.result.createObjectStore(REEL_STORE, { keyPath: "id", autoIncrement: true });
    open.onsuccess = () => resolve(open.result);
    open.onerror = () => reject(open.error);
  });
}

async function reelRun(mode, work) {
  const db = await reelDb();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(REEL_STORE, mode);
      const result = work(tx.objectStore(REEL_STORE));
      tx.oncomplete = () => resolve(result.result ?? result);
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

async function reelAdd(entry) {
  const id = await reelRun("readwrite", store => store.add(entry));
  const keys = await reelRun("readonly", store => store.getAllKeys());
  if (keys.length > REEL_MAX) {
    await reelRun("readwrite", store => store.delete(IDBKeyRange.upperBound(keys[keys.length - REEL_MAX - 1])));
  }
  return id;
}

const reelAll = () => reelRun("readonly", store => store.getAll());
const reelClear = () => reelRun("readwrite", store => store.clear());
