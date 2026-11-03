import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useSettledViewport } from '../useSettledViewport';
import type { MapBounds } from '../../lib/bbox';

const SHERBROOKE: MapBounds = { west: -72.0, south: 45.3, east: -71.8, north: 45.5 };

function shifted(bounds: MapBounds, dLng: number): MapBounds {
  return { ...bounds, west: bounds.west + dLng, east: bounds.east + dLng };
}

describe('useSettledViewport', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('emits the first bounds after the delay', () => {
    const { result } = renderHook(({ b }) => useSettledViewport(b), {
      initialProps: { b: SHERBROOKE as MapBounds | null },
    });
    expect(result.current).toBeNull();
    act(() => vi.advanceTimersByTime(400));
    expect(result.current).toEqual(SHERBROOKE);
  });

  it('emits once after a continuous drag stops', () => {
    const settled: Array<MapBounds | null> = [];
    const { rerender } = renderHook(
      ({ b }) => {
        const value = useSettledViewport(b);
        settled.push(value);
        return value;
      },
      { initialProps: { b: SHERBROOKE } }
    );
    act(() => vi.advanceTimersByTime(400));
    const emittedBefore = new Set(settled.filter(Boolean)).size;

    for (let step = 1; step <= 20; step += 1) {
      rerender({ b: shifted(SHERBROOKE, step * 0.05) });
      act(() => vi.advanceTimersByTime(100));
    }
    act(() => vi.advanceTimersByTime(400));

    const emitted = [...new Set(settled.filter(Boolean))];
    expect(emitted.length - emittedBefore).toBe(1);
    expect(emitted[emitted.length - 1]).toEqual(shifted(SHERBROOKE, 1.0));
  });

  it('does not emit for a nudge under 20%', () => {
    const { result, rerender } = renderHook(({ b }) => useSettledViewport(b), {
      initialProps: { b: SHERBROOKE },
    });
    act(() => vi.advanceTimersByTime(400));
    const first = result.current;
    rerender({ b: shifted(SHERBROOKE, 0.03) });
    act(() => vi.advanceTimersByTime(400));
    expect(result.current).toBe(first);
  });
});
