"""One periodic worker; no API calls when there are no pending changes."""

import logging
import threading

from .store import VaultError


class CommitWorker:
    def __init__(self, store):
        self.store = store
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="vault-write-commits", daemon=True)

    def tick(self):
        if self.store.settings.sync.enabled:
            result = self.store.sync()
            if result["state"] in ("error", "blocked", "conflict"):
                logging.warning("Git sync is %s; writer branch remains writable.", result["state"])
            return result
        if self.store.settings.commit.mode == "interval":
            self.store.flush()
        return self.store.push()

    def _run(self):
        while not self._stop.wait(self.store.settings.commit.interval_seconds):
            try:
                self.tick()
            except VaultError as exc:
                logging.error("Periodic Git operation failed (%s); retrying next interval.", exc.code)
            except Exception:
                logging.error("Periodic Git operation failed; retrying next interval.")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join()
