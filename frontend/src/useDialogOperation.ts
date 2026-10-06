import { useEffect, useRef, useState } from 'react';

export function useDialogOperation() {
  const mounted = useRef(true);
  const revision = useRef(0);
  const inFlight = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; revision.current++; };
  }, []);

  const guard = () => {
    const version = revision.current;
    return () => mounted.current && version === revision.current;
  };
  async function run(action: (current: () => boolean) => Promise<void>) {
    if (inFlight.current) return;
    inFlight.current = true; revision.current++; setBusy(true); setError('');
    const current = guard();
    try { await action(current); }
    catch (error) { if (current()) setError(error instanceof Error ? error.message : 'Ошибка операции.'); }
    finally { inFlight.current = false; if (current()) setBusy(false); }
  }
  return { busy, error, setError, guard, run };
}
