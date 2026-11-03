import { useQuery } from '@tanstack/react-query';
import { AxiosError } from 'axios';
import { apiService } from '../../../services/api';
import { Bbox, MapBounds, largestBox, normalizeViewport, roundBboxOutward } from '../lib/bbox';
import type { CollectionStats, DashboardCatalog } from '../types';

export const STATS_STALE_MS = 10 * 60 * 1000;

/** The renderable collection catalog; fetched once per page load. */
export function useDashboardCatalog() {
  return useQuery<DashboardCatalog>({
    queryKey: ['dashboard-catalog'],
    queryFn: () => apiService.getDashboardCollections(),
    staleTime: Infinity,
    retry: 1,
  });
}

/** The bbox sent for stats: the larger half of a split view, snapped outward. */
export function statsBbox(viewport: MapBounds): Bbox {
  return roundBboxOutward(largestBox(normalizeViewport(viewport)));
}

export function statsQueryKey(collection: string | null, viewport: MapBounds | null) {
  return ['dashboard-stats', collection, viewport ? statsBbox(viewport) : null] as const;
}

export function httpStatus(error: unknown): number | undefined {
  return (error as AxiosError | undefined)?.response?.status;
}

/** Retry transient failures only; 4xx (including 429) are shown, not retried. */
function retryTransient(failureCount: number, error: unknown): boolean {
  const status = httpStatus(error);
  return failureCount < 1 && !(status && status >= 400 && status < 500);
}

/** Stats of the focused collection over the settled viewport, cached per key. */
export function useCollectionStats(collection: string | null, viewport: MapBounds | null) {
  return useQuery<CollectionStats>({
    queryKey: statsQueryKey(collection, viewport),
    queryFn: () => apiService.getDashboardStats(collection!, statsBbox(viewport!)),
    enabled: Boolean(collection && viewport),
    staleTime: STATS_STALE_MS,
    retry: retryTransient,
  });
}
