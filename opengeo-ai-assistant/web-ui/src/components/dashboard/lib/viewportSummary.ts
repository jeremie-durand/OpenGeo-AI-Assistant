import type { CatalogCollection, CategorySummaryEntry } from '../types';
import { Bbox, intersectsAny } from './bbox';

/** Order regional collections before global ones, then by title. */
function byRegionalFirst(a: CatalogCollection, b: CatalogCollection): number {
  if (a.is_global !== b.is_global) return a.is_global ? 1 : -1;
  return a.title.localeCompare(b.title);
}

/**
 * Count the collections whose extent intersects the viewport, per category.
 * Empty categories are dropped; categories are ordered by count, then name.
 */
export function summarizeViewport(
  collections: CatalogCollection[],
  viewport: Bbox[]
): CategorySummaryEntry[] {
  const groups = new Map<string, CatalogCollection[]>();
  for (const collection of collections) {
    if (!intersectsAny(viewport, collection.bbox)) continue;
    const group = groups.get(collection.category) ?? [];
    group.push(collection);
    groups.set(collection.category, group);
  }
  return [...groups.entries()]
    .map(([category, members]) => ({
      category,
      count: members.length,
      collections: [...members].sort(byRegionalFirst),
    }))
    .sort((a, b) => b.count - a.count || a.category.localeCompare(b.category));
}
