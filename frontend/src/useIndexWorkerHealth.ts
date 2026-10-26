import { useEffect, useState } from 'react';
import { api } from './api';
import type { IndexWorkerHealth } from './types';

/** Poll independently of archive/status reads that may be waiting for SQLite. */
export function useIndexWorkerHealth() {
  const [health, setHealth] = useState<IndexWorkerHealth | null>(null);
  useEffect(() => {
    let alive = true, pending = false;
    const controller = new AbortController();
    const poll = async () => {
      if (pending) return;
      pending = true;
      try {
        const value = await api<{ semantic: IndexWorkerHealth }>('/api/indexing/health', { signal: controller.signal });
        if (alive) setHealth(value.semantic);
      } catch { if (alive) setHealth(null); }
      finally { pending = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1000);
    return () => { alive = false; controller.abort(); window.clearInterval(timer); };
  }, []);
  return health;
}
