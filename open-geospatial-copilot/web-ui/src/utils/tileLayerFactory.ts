/**
 * Tile Layer Factory
 * 
 * Creates and configures TileLayer instances with optimized settings
 * based o    // High-resolution optical: enable deep zoom
    // Covers: sentinel-2-l2a, landsat-c2-l2, landsat-8-c2-l2, landsat-9-c2-l2, naip, hls (all variants)
    if (collectionLower.includes('sentinel-2') || 
        collectionLower.includes('landsat') ||
        collectionLower.includes('hls') ||
        collectionLower.includes('naip')) {
      minZoom = 6;
      maxZoom = 22; // Maximum zoom for crisp detail
      console.log(`[TileLayerFactory] High-res optical (no TileJSON): zoom range ${minZoom}-${maxZoom}`);
    }
    // MODIS: enforce minimum zoom to avoid 404 errors with large footprints

    import { getCollectionConfig } from './renderingConfig';
    import type { TileJsonResponse } from './tileJsonFetcher';

    export interface TileLayerOptions {
      tileUrl: string;
      collection: string;
      bounds?: number[];
      tilejson?: TileJsonResponse;
      isElevation?: boolean;
      isThermal?: boolean;
      isFire?: boolean;
      customOpacity?: number;
    }

    /**
     * Creates a Leaflet TileLayer with optimized configuration
     * @param options - Configuration options for tile layer
     * @param leaflet - Leaflet library (window.L)
     * @returns Configured Leaflet TileLayer instance
     */
    export function createTileLayer(
      options: TileLayerOptions,
      leaflet: any
    ): any {
      const {
        tileUrl,
        collection,
        bounds,
        tilejson,
        isElevation = false,
        isThermal = false,
        isFire = false,
        customOpacity
      } = options;

      console.log(`[TileLayerFactory] Creating Leaflet tile layer for collection: ${collection}`);

      // Get centralized rendering configuration
      const renderingConfig = getCollectionConfig(collection);
      const collectionLower = collection.toLowerCase();

      // Determine zoom levels
      let minZoom = renderingConfig.minZoom;
      let maxZoom = renderingConfig.maxZoom;
      if (tilejson) {
        if (tilejson.minzoom !== undefined) minZoom = tilejson.minzoom;
        if (tilejson.maxzoom !== undefined) maxZoom = tilejson.maxzoom;
      }

      // Opacity
      let opacity = customOpacity !== undefined ? customOpacity : renderingConfig.opacity;
      if (collectionLower.includes('sentinel-2') || collectionLower.includes('landsat') || collectionLower.includes('hls') || collectionLower.includes('naip')) {
        opacity = Math.max(opacity, 0.98);
      } else if (collectionLower.includes('sentinel-1')) {
        opacity = Math.max(opacity, 0.95);
      } else if (isElevation || collectionLower.includes('dem') || collectionLower.includes('elevation')) {
        opacity = 0.65;
      } else if (isFire || collectionLower.includes('fire') || collectionLower.includes('14a')) {
        opacity = 0.7;
      } else if (isThermal || collectionLower.includes('thermal')) {
        opacity = 1.0;
      } else {
        opacity = Math.max(opacity, 0.85);
      }

      // Leaflet TileLayer options
      const leafletOptions: any = {
        opacity,
        minZoom,
        maxZoom,
        tileSize: renderingConfig.tileSize || 256,
        bounds: bounds && bounds.length === 4 ? [[bounds[1], bounds[0]], [bounds[3], bounds[2]]] : undefined,
        crossOrigin: 'anonymous',  // Required for canvas screenshot compositing
      };

      // Create and return the Leaflet tile layer
      return leaflet.tileLayer(tileUrl, leafletOptions);
    }

    /**
     * Creates multiple Leaflet tile layers for seamless multi-tile rendering
     * @param tiles - Array of tile information with URLs and bounds
     * @param collection - Collection identifier
     * @param leaflet - Leaflet library (window.L)
     * @returns Array of configured Leaflet TileLayer instances
     */
    export async function createMultipleTileLayers(
      tiles: Array<{ tileUrl: string; bounds: number[]; itemId: string; tilejson?: TileJsonResponse }>,
      collection: string,
      leaflet: any
    ): Promise<{ layers: any[]; successCount: number; errorCount: number }> {
      console.log(`[TileLayerFactory] Creating ${tiles.length} Leaflet tile layers for seamless coverage`);
      const layers: any[] = [];
      let successCount = 0;
      let errorCount = 0;
      const isElevation = collection.toLowerCase().includes('dem') || collection.toLowerCase().includes('elevation');
      for (const tile of tiles) {
        try {
          const layer = createTileLayer(
            {
              tileUrl: tile.tileUrl,
              collection,
              bounds: tile.bounds,
              tilejson: tile.tilejson,
              isElevation
            },
            leaflet
          );
          layers.push(layer);
          successCount++;
          console.log(`[TileLayerFactory] Created Leaflet layer ${successCount}/${tiles.length}: ${tile.itemId}`);
        } catch (error) {
          console.error(`[TileLayerFactory] Error creating Leaflet layer for ${tile.itemId}:`, error);
          errorCount++;
        }
      }
      console.log(`[TileLayerFactory] Multi-tile Leaflet layer creation complete. Success: ${successCount}, Errors: ${errorCount}`);
      return { layers, successCount, errorCount };
    }

/**
 * Validates and clamps bounds to prevent geometry extent errors
 * 
 * @param bounds - [west, south, east, north]
 * @returns Validated and clamped bounds or undefined if invalid
 */
export function validateAndClampBounds(bounds: number[]): number[] | undefined {
  if (!bounds || !Array.isArray(bounds) || bounds.length !== 4) {
    console.warn('[TileLayerFactory] Invalid bounds array:', bounds);
    return undefined;
  }

  const [west, south, east, north] = bounds;

  // Validate each coordinate is a valid number
  const isValidBound = (coord: any) => {
    return coord !== null && 
           coord !== undefined && 
           typeof coord === 'number' && 
           !isNaN(coord) && 
           isFinite(coord) &&
           coord >= -180 && 
           coord <= 180;
  };

  if (!isValidBound(west) || !isValidBound(south) || 
      !isValidBound(east) || !isValidBound(north)) {
    console.warn('[TileLayerFactory] Invalid bound coordinates:', bounds);
    return undefined;
  }

  if (west >= east || south >= north) {
    console.warn('[TileLayerFactory] Invalid bounds geometry (west >= east or south >= north):', bounds);
    return undefined;
  }

  // Clamp to safe ranges (WebMercator latitude limit ~85.05°)
  const WEB_MERCATOR_LAT = 85.05;
  const clampedWest = Math.max(-180, Math.min(180, west));
  const clampedSouth = Math.max(-WEB_MERCATOR_LAT, Math.min(WEB_MERCATOR_LAT, south));
  const clampedEast = Math.max(-180, Math.min(180, east));
  const clampedNorth = Math.max(-WEB_MERCATOR_LAT, Math.min(WEB_MERCATOR_LAT, north));

  // Post-clamp validation: reject degenerate bounds (e.g., S90 tiles clamp to south=north=-85)
  if (clampedSouth >= clampedNorth) {
    console.warn('[TileLayerFactory] Bounds degenerate after WebMercator clamping (south >= north):', 
      { original: bounds, clamped: [clampedWest, clampedSouth, clampedEast, clampedNorth] });
    return undefined;
  }
  if (clampedWest >= clampedEast) {
    console.warn('[TileLayerFactory] Bounds degenerate after clamping (west >= east):', 
      { original: bounds, clamped: [clampedWest, clampedSouth, clampedEast, clampedNorth] });
    return undefined;
  }

  const clamped = [clampedWest, clampedSouth, clampedEast, clampedNorth];

  // Log if clamping occurred
  if (JSON.stringify(bounds) !== JSON.stringify(clamped)) {
    console.log('[TileLayerFactory] Bounds clamped:', { original: bounds, clamped });
  }

  return clamped;
}

/**
 * Ensures tile URL includes high-resolution parameter
 * 
 * @param tileUrl - Original tile URL
 * @returns URL with tile_scale=2 parameter (only for TiTiler URLs, not native tiles)
 */
export function ensureHighResolution(tileUrl: string): string {
  // Check if tile_scale parameter already exists
  if (tileUrl.includes('tile_scale=')) {
    return tileUrl;
  }

  // CRITICAL: Do NOT add tile_scale=2 to Planetary Computer native tiles URLs
  // Native tiles API (/api/data/v1/item/tiles/) does NOT support tile_scale parameter
  // Adding it causes 404 errors
  const isNativeTiles = tileUrl.includes('/api/data/v1/item/tiles/');
  
  if (isNativeTiles) {
    console.log('[TileLayerFactory] Native tiles detected - NOT adding tile_scale (would cause 404)');
    return tileUrl;
  }

  // For TiTiler URLs, add tile_scale=2 for high-resolution rendering
  const separator = tileUrl.includes('?') ? '&' : '?';
  const highResUrl = `${tileUrl}${separator}tile_scale=2`;
  
  console.log('[TileLayerFactory] Added tile_scale=2 for high-resolution rendering');
  
  return highResUrl;
}

/**
 * Determines if collection is elevation data
 * 
 * @param collection - Collection identifier
 * @returns true if elevation/DEM data
 */
export function isElevationCollection(collection: string): boolean {
  const lower = collection.toLowerCase();
  return lower.includes('dem') || 
         lower.includes('elevation') || 
         lower.includes('nasadem') ||
         lower.includes('copernicus');
}

/**
 * Determines if collection is thermal data
 * 
 * @param collection - Collection identifier
 * @returns true if thermal infrared data
 */
export function isThermalCollection(collection: string): boolean {
  const lower = collection.toLowerCase();
  return lower.includes('thermal') || 
         lower.includes('modis-11') ||
         lower.includes('lst'); // Land Surface Temperature
}

/**
 * Determines if collection is fire data
 * 
 * @param collection - Collection identifier
 * @returns true if fire detection data
 */
export function isFireCollection(collection: string): boolean {
  const lower = collection.toLowerCase();
  return lower.includes('fire') || 
         lower.includes('modis-14');
}
