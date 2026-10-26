import React from 'react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { apiService } from '../../../../services/api';
import { statsQueryKey, useCollectionStats } from '../useDashboardData';
import type { MapBounds } from '../../lib/bbox';

const SHERBROOKE: MapBounds = { west: -71.96, south: 45.32, east: -71.81, north: 45.47 };

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

afterEach(() => vi.restoreAllMocks());

describe('statsQueryKey', () => {
  it('gives two nearby viewports the same key', () => {
    const nearby = { west: -71.94, south: 45.31, east: -71.82, north: 45.48 };
    expect(statsQueryKey('sentinel-2-l2a', SHERBROOKE)).toEqual(
      statsQueryKey('sentinel-2-l2a', nearby)
    );
  });

  it('gives a moved viewport a new key', () => {
    const moved = { west: -73.6, south: 45.4, east: -73.4, north: 45.6 };
    expect(statsQueryKey('sentinel-2-l2a', SHERBROOKE)).not.toEqual(
      statsQueryKey('sentinel-2-l2a', moved)
    );
  });
});

describe('useCollectionStats', () => {
  it('makes no request without a focused collection', async () => {
    const spy = vi.spyOn(apiService, 'getDashboardStats');
    const { result } = renderHook(() => useCollectionStats(null, SHERBROOKE), { wrapper });
    await waitFor(() => expect(result.current.fetchStatus).toBe('idle'));
    expect(spy).not.toHaveBeenCalled();
  });

  it('requests the snapped bbox for the focused collection', async () => {
    const spy = vi.spyOn(apiService, 'getDashboardStats').mockResolvedValue({
      collection: 'sentinel-2-l2a',
      bbox: [-72, 45.3, -71.8, 45.5],
      scene_count: 3,
      capped: false,
      static: false,
      latest_datetime: null,
    });
    const { result } = renderHook(() => useCollectionStats('sentinel-2-l2a', SHERBROOKE), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(spy).toHaveBeenCalledWith('sentinel-2-l2a', [-72, 45.3, -71.8, 45.5]);
  });
});
