import { describe, it, expect } from 'vitest';
import {
  Bbox,
  MapBounds,
  intersects,
  intersectsAny,
  isGlobal,
  largestBox,
  movedEnough,
  normalizeViewport,
  roundBboxOutward,
} from '../bbox';

const SHERBROOKE: MapBounds = { west: -72.0, south: 45.3, east: -71.8, north: 45.5 };

describe('normalizeViewport', () => {
  it('keeps a normal view as one box', () => {
    expect(normalizeViewport(SHERBROOKE)).toEqual([[-72.0, 45.3, -71.8, 45.5]]);
  });

  it('splits a view crossing the antimeridian', () => {
    const boxes = normalizeViewport({ west: 170, south: -20, east: 190, north: 0 });
    expect(boxes).toEqual([
      [170, -20, 180, 0],
      [-180, -20, -170, 0],
    ]);
  });

  it('shifts a wrapped view back into range', () => {
    const [box] = normalizeViewport({ west: 288, south: 45.3, east: 288.2, north: 45.5 });
    expect(box[0]).toBeCloseTo(-72);
    expect(box[2]).toBeCloseTo(-71.8);
  });

  it('returns the whole world for a view wider than 360 degrees', () => {
    expect(normalizeViewport({ west: -300, south: -95, east: 300, north: 95 })).toEqual([
      [-180, -90, 180, 90],
    ]);
  });
});

describe('intersects', () => {
  const usa: Bbox = [-125, 24, -66, 50];

  it('matches overlapping and touching boxes', () => {
    expect(intersects([-72, 45.3, -71.8, 45.5], [-80, 40, -60, 50])).toBe(true);
    expect(intersects([-66, 40, -60, 45], usa)).toBe(true);
  });

  it('rejects disjoint boxes', () => {
    expect(intersects([2.2, 48.8, 2.5, 48.9], usa)).toBe(false);
  });

  it('counts a collection on either side of a split view', () => {
    const split = normalizeViewport({ west: 170, south: -20, east: 190, north: 0 });
    expect(intersectsAny(split, [-175, -30, -160, 10])).toBe(true);
    expect(intersectsAny(split, [0, -30, 10, 10])).toBe(false);
  });
});

describe('movedEnough', () => {
  it('is true without a previous view', () => {
    expect(movedEnough(null, SHERBROOKE)).toBe(true);
  });

  it('ignores a nudge under 20% of the view', () => {
    const nudged = { ...SHERBROOKE, west: -71.97, east: -71.77 };
    expect(movedEnough(SHERBROOKE, nudged)).toBe(false);
  });

  it('detects a pan of 20% or more', () => {
    const panned = { ...SHERBROOKE, west: -71.95, east: -71.75 };
    expect(movedEnough(SHERBROOKE, panned)).toBe(true);
  });

  it('detects a zoom out', () => {
    const zoomedOut = { west: -72.1, south: 45.2, east: -71.7, north: 45.6 };
    expect(movedEnough(SHERBROOKE, zoomedOut)).toBe(true);
  });
});

describe('roundBboxOutward', () => {
  it('snaps outward to a 0.1 degree grid', () => {
    expect(roundBboxOutward([-71.96, 45.32, -71.81, 45.47])).toEqual([-72, 45.3, -71.8, 45.5]);
  });

  it('gives nearby views the same key', () => {
    const a = roundBboxOutward([-71.96, 45.32, -71.81, 45.47]);
    const b = roundBboxOutward([-71.94, 45.31, -71.82, 45.48]);
    expect(a).toEqual(b);
  });

  it('keeps grid-aligned values unchanged', () => {
    expect(roundBboxOutward([-72, 45.3, -71.8, 45.5])).toEqual([-72, 45.3, -71.8, 45.5]);
  });
});

describe('isGlobal and largestBox', () => {
  it('flags whole-world extents only', () => {
    expect(isGlobal([-180, -90, 180, 90])).toBe(true);
    expect(isGlobal([-125, 24, -66, 50])).toBe(false);
  });

  it('picks the larger half of a split view', () => {
    expect(
      largestBox([
        [170, -20, 180, 0],
        [-180, -20, -160, 0],
      ])
    ).toEqual([-180, -20, -160, 0]);
  });
});
