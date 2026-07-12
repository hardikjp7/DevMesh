(async function () {

    const DB_NAME = "webpDB";
    const DB_VERSION = 1;

    function openDB(storeName) {
        return new Promise((resolve, reject) => {

            const request = indexedDB.open(DB_NAME, DB_VERSION);

            request.onupgradeneeded = () => {
                const db = request.result;

                if (!db.objectStoreNames.contains(storeName)) {
                    db.createObjectStore(storeName);
                }
            };

            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    async function setItem(key, value, storeName) {
        const db = await openDB(storeName);

        return new Promise((resolve, reject) => {
            const tx = db.transaction(storeName, "readwrite");

            tx.objectStore(storeName).put(value, key);

            tx.oncomplete = resolve;
            tx.onerror = () => reject(tx.error);
        });
    }

    async function getItem(key, storeName) {
        const db = await openDB(storeName);

        return new Promise((resolve, reject) => {
            const tx = db.transaction(storeName, "readonly");

            const req = tx.objectStore(storeName).get(key);

            req.onsuccess = () => resolve(req.result ?? null);
            req.onerror = () => reject(req.error);
        });
    }

    async function removeItem(key, storeName) {
        const db = await openDB(storeName);

        return new Promise((resolve, reject) => {
            const tx = db.transaction(storeName, "readwrite");

            tx.objectStore(storeName).delete(key);

            tx.oncomplete = resolve;
            tx.onerror = () => reject(tx.error);
        });
    }

    async function containsKey(key, storeName) {
        const db = await openDB(storeName);

        return new Promise((resolve, reject) => {
            const tx = db.transaction(storeName, "readonly");

            const store = tx.objectStore(storeName);
            const request = store.get(key);

            request.onsuccess = () => {
                if (request.result !== undefined) {
                    resolve(true);
                } else {
                    resolve(false);
                }
            };

            request.onerror = () => {
                reject(request.error);
            };
        });
    }

    window.storageService = {
        setItem,
        getItem,
        removeItem,
        containsKey
    };

})();