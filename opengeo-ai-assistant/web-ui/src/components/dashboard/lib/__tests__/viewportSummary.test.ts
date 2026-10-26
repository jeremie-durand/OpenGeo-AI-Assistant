import { describe, it, expect } from 'vitest';
import { summarizeViewport } from '../viewportSummary';
import type { CatalogCollection } from '../../types';
import type { Bbox } from '../bbox';

const WORLD: Bbox = [-180, -90, 180, 90];
const NORTH_AMERICA: Bbox = [-170, 10, -50, 80];
const EUROPE: Bbox = [-25, 34, 45, 72];
const SHERBROOKE: Bbox[] = [[-72.0, 45.3, -71.8, 45.5]];

function collection(id: string, category: string, bbox: Bbox, title = id): CatalogCollection {
  return { id, title, category, bbox, is_global: bbox === WORLD };
}

const CATALOG = [
  collection('sentinel-2-l2a', 'Optical', WORLD, 'Sentinel-2'),
  collection('naip', 'Optical', NORTH_AMERICA, 'NAIP'),
  collection('landsat-c2-l2', 'Optical', WORLD, 'Landsat'),
  collection('cop-dem-glo-30', 'Elevation', WORLD),
  collection('eu-only', 'Land cover', EUROPE),
];

describe('summarizeViewport', () => {
  it('counts covering collections per category and drops empty ones', () => {
    const summary = summarizeViewport(CATALOG, SHERBROOKE);
    expect(summary.map((s) => [s.category, s.count])).toEqual([
      ['Optical', 3],
      ['Elevation', 1],
    ]);
  });

  it('lists regional collections before global ones', () => {
    const [optical] = summarizeViewport(CATALOG, SHERBROOKE);
    expect(optical.collections.map((c) => c.id)).toEqual([
      'naip',
      'landsat-c2-l2',
      'sentinel-2-l2a',
    ]);
  });

  it('returns nothing for an empty catalog', () => {
    expect(summarizeViewport([], SHERBROOKE)).toEqual([]);
  });
});
