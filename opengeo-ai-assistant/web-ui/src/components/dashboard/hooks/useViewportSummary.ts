import { useMemo } from 'react';
import { MapBounds, normalizeViewport } from '../lib/bbox';
import { summarizeViewport } from '../lib/viewportSummary';
import type { CatalogCollection, CategorySummaryEntry } from '../types';

/** Category counts of the collections covering the settled viewport. */
export function useViewportSummary(
  collections: CatalogCollection[] | undefined,
  viewport: MapBounds | null
): CategorySummaryEntry[] {
  return useMemo(() => {
    if (!collections || !viewport) return [];
    return summarizeViewport(collections, normalizeViewport(viewport));
  }, [collections, viewport]);
}
