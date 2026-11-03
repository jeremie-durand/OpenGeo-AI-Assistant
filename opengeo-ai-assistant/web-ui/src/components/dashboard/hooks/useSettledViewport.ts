import { useEffect, useRef, useState } from 'react';
import { MapBounds, movedEnough } from '../lib/bbox';

export const SETTLE_DELAY_MS = 400;

/**
 * Emit the map bounds once the map has stopped moving for `delay` ms, and only
 * when they moved at least 20% from the last emitted bounds.
 */
export function useSettledViewport(
  bounds: MapBounds | null | undefined,
  delay = SETTLE_DELAY_MS
): MapBounds | null {
  const [settled, setSettled] = useState<MapBounds | null>(null);
  const lastEmitted = useRef<MapBounds | null>(null);
  const { west, south, east, north } = bounds ?? ({} as Partial<MapBounds>);

  useEffect(() => {
    if (west === undefined || south === undefined || east === undefined || north === undefined) {
      return;
    }
    const next = { west, south, east, north };
    const timer = setTimeout(() => {
      if (movedEnough(lastEmitted.current, next)) {
        lastEmitted.current = next;
        setSettled(next);
      }
    }, delay);
    return () => clearTimeout(timer);
  }, [west, south, east, north, delay]);

  return settled;
}
