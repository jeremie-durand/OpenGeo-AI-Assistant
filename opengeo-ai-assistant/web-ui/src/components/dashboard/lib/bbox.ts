/** Bounding box as [west, south, east, north] in degrees. */
export type Bbox = [number, number, number, number];

/** Map bounds as reported by MapView in mapContext.bounds. */
export interface MapBounds {
  north: number;
  south: number;
  east: number;
  west: number;
}

const GLOBAL_MIN_WIDTH = 350;
const GLOBAL_MIN_HEIGHT = 160;

const clampLat = (lat: number) => Math.max(-90, Math.min(90, lat));

/**
 * Convert Leaflet bounds to one or two boxes inside [-180, 180].
 * Leaflet reports longitudes beyond ±180 after wrapping, so a view crossing
 * the antimeridian is split into a box on each side.
 */
export function normalizeViewport(bounds: MapBounds): Bbox[] {
  const south = clampLat(bounds.south);
  const north = clampLat(bounds.north);
  if (bounds.east - bounds.west >= 360) return [[-180, south, 180, north]];

  const shift = 360 * Math.floor((bounds.west + 180) / 360);
  const west = bounds.west - shift;
  const east = bounds.east - shift;
  if (east <= 180) return [[west, south, east, north]];
  return [
    [west, south, 180, north],
    [-180, south, east - 360, north],
  ];
}

export function intersects(a: Bbox, b: Bbox): boolean {
  return a[0] <= b[2] && a[2] >= b[0] && a[1] <= b[3] && a[3] >= b[1];
}

export function intersectsAny(boxes: Bbox[], target: Bbox): boolean {
  return boxes.some((box) => intersects(box, target));
}

/** The box with the largest area, used when a split view needs a single bbox. */
export function largestBox(boxes: Bbox[]): Bbox {
  const area = (b: Bbox) => (b[2] - b[0]) * (b[3] - b[1]);
  return boxes.reduce((best, box) => (area(box) > area(best) ? box : best));
}

/**
 * True when any edge moved by at least `threshold` of the previous view's
 * width (east/west) or height (north/south). Zooming moves every edge, so a
 * zoom that changes the extent by the threshold also counts.
 */
export function movedEnough(prev: MapBounds | null, next: MapBounds, threshold = 0.2): boolean {
  if (!prev) return true;
  const width = prev.east - prev.west;
  const height = prev.north - prev.south;
  return (
    Math.abs(next.west - prev.west) >= threshold * width ||
    Math.abs(next.east - prev.east) >= threshold * width ||
    Math.abs(next.south - prev.south) >= threshold * height ||
    Math.abs(next.north - prev.north) >= threshold * height
  );
}

const round = (value: number) => Number(value.toFixed(6));

/** Snap a bbox outward to a `step`-degree grid so nearby views share a cache key. */
export function roundBboxOutward(bbox: Bbox, step = 0.1): Bbox {
  const down = (v: number) => round(Math.floor(round(v / step)) * step);
  const up = (v: number) => round(Math.ceil(round(v / step)) * step);
  return [
    Math.max(-180, down(bbox[0])),
    Math.max(-90, down(bbox[1])),
    Math.min(180, up(bbox[2])),
    Math.min(90, up(bbox[3])),
  ];
}

export function isGlobal(bbox: Bbox): boolean {
  return bbox[2] - bbox[0] >= GLOBAL_MIN_WIDTH && bbox[3] - bbox[1] >= GLOBAL_MIN_HEIGHT;
}
