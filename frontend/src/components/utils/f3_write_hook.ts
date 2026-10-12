import { useCallback, useMemo, useRef, useState } from 'react';

import { F3RunResult, WriteGate } from '@components/utils/f3_write';

/**
 * Envoltorio de `WriteGate` para componentes: mantiene el estado pendiente y expone `inflight` para
 * deshabilitar el botón mientras la escritura está en vuelo (evita el doble clic).
 */
export function useF3Write() {
  const gate = useMemo(() => new WriteGate(), []);
  const [inflight, setInflight] = useState(false);
  const mounted = useRef(true);

  const run = useCallback(
    async <T,>(scope: string, perform: (key: string) => Promise<T>): Promise<F3RunResult<T>> => {
      if (gate.isInflight) {
        return { status: 'ignored' };
      }
      if (mounted.current) {
        setInflight(true);
      }
      try {
        return await gate.run<T>(scope, perform);
      } finally {
        if (mounted.current) {
          setInflight(false);
        }
      }
    },
    [gate]
  );

  const reset = useCallback(() => {
    gate.reset();
    setInflight(false);
  }, [gate]);

  return { run, reset, inflight, gate };
}

export default useF3Write;
