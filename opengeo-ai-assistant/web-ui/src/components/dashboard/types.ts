import type { Bbox } from './lib/bbox';

/** One renderable collection from GET /api/dashboard/collections. */
export interface CatalogCollection {
  id: string;
  title: string;
  category: string;
  bbox: Bbox;
  is_global: boolean;
}

export interface DashboardCatalog {
  collections: CatalogCollection[];
  fetched_at: number;
}

/** A category of the viewport summary and the collections it counts. */
export interface CategorySummaryEntry {
  category: string;
  count: number;
  collections: CatalogCollection[];
}

export interface MonthCount {
  month: string;
  count: number;
}

/** Response of POST /api/dashboard/stats. */
export interface CollectionStats {
  collection: string;
  bbox: Bbox;
  scene_count: number;
  capped: boolean;
  static: boolean;
  latest_datetime: string | null;
  monthly?: MonthCount[];
  cloud_cover?: number[];
}

/** Dashboard context sent with each /api/query request. */
export interface DashboardContext {
  collection: string;
  title: string;
  bbox: Bbox;
  scene_count: number;
  capped: boolean;
  monthly: MonthCount[];
  cloud_cover?: number[];
  latest_datetime: string | null;
}
