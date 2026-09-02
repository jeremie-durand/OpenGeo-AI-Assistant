// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.
// Pin button relocated to top-left - cleaned up duplicates

import React, { useEffect, useState, useRef } from 'react';
import { Dataset, API_BASE_URL } from '../services/api';
import { authenticatedFetch } from '../services/authHelper';
// TileUrlGenerator removed - using backend-only tile URL generation (MPC best practice)
import { getCollectionVisualization, getCollectionConfig } from '../config/collectionConfig';
import { getCollectionConfig as getRenderingConfig } from '../utils/renderingConfig';
import { fetchAndSignTileJSON, fetchMultipleTileJSON } from '../utils/tileJsonFetcher';
import {
  createTileLayer,
  createMultipleTileLayers,
  validateAndClampBounds,
} from '../utils/tileLayerFactory';
import {
  isMultiTileData,
  isSingleTileData,
  getCollection,
  getBoundingBox,
  prepareMultiTileData,
  prepareSingleTileData,
  isElevationData,
  isThermalData,
  isFireData,
  validateSatelliteData,
  applyAssetFixes,
} from '../utils/satelliteDataHelpers';
import {
  logRenderingStart,
  logRenderingComplete,
  logTileJsonFetch,
  logTileLayerCreated,
  logError,
  logWarning,
  logDEMDetection,
  logSymbolLayerSuppression,
  startPerformanceTracking,
  endPerformanceTracking,
} from '../utils/renderingLogger';
import DataLegend from './DataLegend';
import { useT } from '../i18n/I18nContext';

// Centralized map provider configuration - set to 'leaflet' for open-source, no-auth setup
const mapProvider = 'leaflet';

/**
 * Extract geographic region from query text and return appropriate bounds
 * NOTE: This function now relies on backend location resolution instead of hardcoded coordinates
 */
function extractGeographicRegion(
  queryText: string
): { west: number; south: number; east: number; north: number } | null {
  if (!queryText) return null;

  // The backend's dynamic location resolution handles all location queries
  // This frontend function is kept for legacy compatibility but should not be used
  console.log(
    '?? MapView: Frontend region extraction bypassed - using backend location resolution'
  );
  return null;
}

// Declare global objects for TypeScript
declare global {
  interface Window {
    atlas: any;
    L: any; // Leaflet
    MapDebugger?: any;
    STACDebugger?: any;
    enableMapDebugging?: (map: any) => void;
    testKnownWorkingQuery?: () => Promise<any>;
    downloadDebugReport?: () => void;
  }
}

interface MapViewProps {
  selectedDataset: Dataset | null;
  lastChatResponse?: any;
  onPinChange?: (pin: { lat: number; lng: number } | null) => void;
  onMobilityAnalysisRequested?: () => void; // New: when pin button is clicked
  onGeointAnalysis?: (result: any) => void;
  onMapContextChange?: (context: any) => void; // New: provides map context for Chat Vision
  onModulesMenuOpen?: () => void; // New: when modules menu opens
  onModuleSelected?: (module: string | null) => void; // When a module is selected or deselected (null = deselected)
  onToggleSidebar?: () => void; // New: toggle data catalog sidebar
  sidebarOpen?: boolean; // New: current state of sidebar
  comparisonUserQuery?: string | null; // New: user's comparison query to process
  onTerrainSessionChange?: (
    session: { sessionId: string | null; lat: number; lng: number } | null
  ) => void; // Terrain session for multi-turn chat
}

interface SatelliteData {
  bbox?: number[];
  items: Array<{
    id: string;
    collection: string;
    datetime: string;
    preview?: string;
    tile_url?: string;
    assets?: any;
  }>;
  preview_url?: string;
  tile_url?: string;
  thermal_mode?: boolean;
  thermal_timestamp?: number;
  all_tile_urls?: Array<{
    item_id: string;
    bbox: number[];
    tilejson_url: string;
  }>;
  // Mosaic support for seamless composited tiles
  is_mosaic?: boolean;
  mosaic_search_id?: string;
}

/**
 * MapView Component
 *
 * Interactive satellite map with pin-based mobility analysis.
 *
 * Workflow:
 * 1. User clicks "Drop Pin" -> triggers onMobilityAnalysisRequested()
 * 2. Chat shows: "Dropping a pin will produce mobility analysis"
 * 3. User clicks map -> pin placed, coordinates stored
 * 4. Automatically triggers mobility analysis
 * 5. Results displayed in chat
 */
const MapView: React.FC<MapViewProps> = ({
  selectedDataset,
  lastChatResponse,
  onPinChange,
  onMobilityAnalysisRequested,
  onGeointAnalysis,
  onMapContextChange,
  onModulesMenuOpen,
  onModuleSelected,
  onToggleSidebar,
  sidebarOpen = false,
  comparisonUserQuery = null,
  onTerrainSessionChange,
}) => {
  const t = useT();
  const mapRef = useRef<HTMLDivElement>(null);
  const [map, setMap] = useState<any>(null);
  const [mapLoaded, setMapLoaded] = useState(false);
  const [satelliteData, setSatelliteData] = useState<SatelliteData | null>(null);
  const [currentLayer, setCurrentLayer] = useState<any>(null);
  const [mapError, setMapError] = useState<string | null>(null);
  const [showStyleTip, setShowStyleTip] = useState<boolean>(false);
  const [isThermalMode, setIsThermalMode] = useState<boolean>(false);

  // Track map camera position changes so map context re-computes after navigate_to
  const [mapPositionVersion, setMapPositionVersion] = useState<number>(0);

  // Dynamic tile expansion state
  const [originalBounds, setOriginalBounds] = useState<number[] | null>(null);
  const [lastCollection, setLastCollection] = useState<string | null>(null);
  const [isExpanding, setIsExpanding] = useState<boolean>(false);

  // Elevation legend state
  const [showDataLegend, setShowDataLegend] = useState<boolean>(false);

  // Pin state for location-based GEOINT analysis
  const [pinMode, setPinMode] = useState<boolean>(false);
  const [selectedModule, setSelectedModule] = useState<string | null>(null); // 'terrain', 'mobility', 'building_damage'
  const [showModulesMenu, setShowModulesMenu] = useState<boolean>(false);
  const [analysisInProgress, setAnalysisInProgress] = useState<boolean>(false);
  const analysisAbortControllerRef = useRef<AbortController | null>(null);
  const [pinState, setPinState] = useState<{
    lat: number | null;
    lng: number | null;
    active: boolean;
    marker: any | null;
  }>({
    lat: null,
    lng: null,
    active: false,
    marker: null,
  });

  // Mobility two-pin A->B state
  const [mobilityPinA, setMobilityPinA] = useState<{
    lat: number;
    lng: number;
    marker: any;
  } | null>(null);
  const [mobilityPinB, setMobilityPinB] = useState<{
    lat: number;
    lng: number;
    marker: any;
  } | null>(null);
  // Refs to avoid stale closure in map click handler — refs are always current
  const mobilityPinARef = useRef<{ lat: number; lng: number; marker: any } | null>(null);
  const mobilityPinBRef = useRef<{ lat: number; lng: number; marker: any } | null>(null);

  // Terrain analysis state
  const [terrainAnalysisMode, setTerrainAnalysisMode] = useState<boolean>(false);
  const [terrainAnalysisPin, setTerrainAnalysisPin] = useState<{
    lat: number | null;
    lng: number | null;
    marker: any | null;
  }>({
    lat: null,
    lng: null,
    marker: null,
  });
  // Terrain session for multi-turn conversation
  const [terrainSessionId, setTerrainSessionId] = useState<string | null>(null);

  // Vision mode state - NEW: explicit vision analysis mode
  const [visionMode, setVisionMode] = useState<boolean>(false);
  const [visionPin, setVisionPin] = useState<{
    lat: number | null;
    lng: number | null;
  }>({
    lat: null,
    lng: null,
  });
  // Vision screenshot - captured when pin is placed, sent with queries
  const [visionScreenshot, setVisionScreenshot] = useState<string | null>(null);

  // Comparison module state
  const [comparisonMode, setComparisonMode] = useState<boolean>(false);
  const [comparisonState, setComparisonState] = useState<{
    awaitingUserQuery: boolean;
    beforeImagery: any | null;
    afterImagery: any | null;
    beforeScreenshot: string | null;
    afterScreenshot: string | null;
    showingBefore: boolean;
  }>({
    awaitingUserQuery: false,
    beforeImagery: null,
    afterImagery: null,
    beforeScreenshot: null,
    afterScreenshot: null,
    showingBefore: true, // Default to showing "before" view
  });

  // Zoom level tracking state
  const [currentZoomLevel, setCurrentZoomLevel] = useState<number>(5);

  // Initialize Leaflet map
  useEffect(() => {
    if (!mapRef.current || map) return;
    if (typeof window !== 'undefined' && window.L) {
      try {
        const leafletMap = window.L.map(mapRef.current, {
          center: [52.0, -71.5],
          zoom: 5,
          zoomControl: true,
        });
        window.L.tileLayer(
          'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
          {
            attribution: '© Esri World Imagery',
            maxZoom: 22,
            crossOrigin: 'anonymous',
          }
        ).addTo(leafletMap);
        setMap(leafletMap);
        setMapLoaded(true);
        setMapError(null);
        console.log('MapView: Leaflet map initialized successfully');
      } catch (error) {
        console.error('MapView: Failed to initialize Leaflet map:', error);
        setMapError('Failed to initialize map');
      }
    }
  }, [mapRef, map]);

  // Helper function to test tile URL at specific coordinates (Leaflet only)
  const testTileUrl = async (
    tileTemplate: string,
    z: number,
    x: number,
    y: number
  ): Promise<void> => {
    const testUrl = tileTemplate
      .replace('{z}', z.toString())
      .replace('{x}', x.toString())
      .replace('{y}', y.toString());
    console.log(`MapView: [TILE-TEST] Testing tile at ${z}/${x}/${y}: ${testUrl}`);
    try {
      const response = await fetch(testUrl);
      console.log(`MapView: [TILE-TEST] Response status: ${response.status}`);
      if (response.ok) {
        const blob = await response.blob();
        console.log(
          `MapView: [TILE-TEST] Success! Blob size: ${blob.size} bytes, type: ${blob.type}`
        );
      } else {
        console.log(`MapView: [TILE-TEST] Failed with status ${response.status}`);
      }
    } catch (error) {
      console.error(`MapView: [TILE-TEST] Error:`, error);
    }
  };

  /**
   * Capture Map Screenshot for Chat Vision Analysis
   *
   * Captures the current map view as a base64-encoded PNG image.
   * This is used when users ask questions about the visible imagery
   * (e.g., "What bodies of water are in this image?")
   */
  const captureMapScreenshot = (): Promise<string | null> => {
    return new Promise((resolve) => {
      try {
        console.log('[SNAP] MapView: Starting screenshot capture...');

        const mapContainer = mapRef.current;
        if (!mapContainer) {
          console.warn('MapView: Cannot capture screenshot - map container not found');
          resolve(null);
          return;
        }

        // Leaflet renders tiles as <img> elements, not onto a <canvas>.
        // We composite all tile images onto an offscreen canvas.
        const containerRect = mapContainer.getBoundingClientRect();
        const width = Math.round(containerRect.width);
        const height = Math.round(containerRect.height);

        if (width === 0 || height === 0) {
          console.warn('MapView: Map container has zero size');
          resolve(null);
          return;
        }

        const offscreen = document.createElement('canvas');
        offscreen.width = width;
        offscreen.height = height;
        const ctx = offscreen.getContext('2d');
        if (!ctx) {
          resolve(null);
          return;
        }

        // Dark background (matches map theme)
        ctx.fillStyle = '#1a1a2e';
        ctx.fillRect(0, 0, width, height);

        // Draw all loaded tile <img> elements at their screen positions
        let drawn = 0;
        const imgs = mapContainer.querySelectorAll<HTMLImageElement>('img');
        console.log(`[SNAP] Found ${imgs.length} img elements in map container`);

        imgs.forEach((img) => {
          if (!img.complete || img.naturalWidth === 0) return;
          const r = img.getBoundingClientRect();
          // Skip images outside the container viewport
          if (
            r.right <= containerRect.left ||
            r.left >= containerRect.right ||
            r.bottom <= containerRect.top ||
            r.top >= containerRect.bottom
          )
            return;
          const x = Math.round(r.left - containerRect.left);
          const y = Math.round(r.top - containerRect.top);
          const w = Math.round(r.width);
          const h = Math.round(r.height);
          try {
            ctx.drawImage(img, x, y, w, h);
            drawn++;
          } catch (e) {
            // Skip CORS-blocked image
          }
        });

        console.log(`[SNAP] Composited ${drawn} tile images onto ${width}x${height} canvas`);

        // Also composite any canvas overlay layers (vector renderers, etc.)
        const canvases = mapContainer.querySelectorAll<HTMLCanvasElement>('canvas');
        canvases.forEach((c) => {
          if (c.width === 0 || c.height === 0) return;
          const r = c.getBoundingClientRect();
          try {
            ctx.drawImage(
              c,
              Math.round(r.left - containerRect.left),
              Math.round(r.top - containerRect.top)
            );
          } catch (e) {}
        });

        if (drawn === 0) {
          console.warn('[SNAP] No tile images could be drawn — tiles may still be loading');
          resolve(null);
          return;
        }

        try {
          const dataURL = offscreen.toDataURL('image/jpeg', 0.85);
          if (dataURL && dataURL.length > 5000) {
            console.log(`[SNAP] Screenshot succeeded (${Math.round(dataURL.length / 1024)}KB)`);
            resolve(dataURL);
          } else {
            console.warn(`[SNAP] Screenshot too small (${dataURL?.length || 0} bytes)`);
            resolve(null);
          }
        } catch (e) {
          // toDataURL throws SecurityError if canvas is tainted by cross-origin images
          // Fix: add crossOrigin: 'anonymous' to tile layers so images are CORS-enabled
          console.error('[SNAP] Canvas tainted by cross-origin image — cannot export:', e);
          resolve(null);
        }
      } catch (error) {
        console.error('MapView: Error in screenshot setup:', error);
        resolve(null);
      }
    });
  };

  // Auto-hide "Adjusting tiles to zoom level" indicator after 3 seconds
  useEffect(() => {
    if (isExpanding) {
      const timer = setTimeout(() => {
        console.log('MapView: Auto-hiding tile expansion indicator after 3s');
        setIsExpanding(false);
      }, 3000); // Hide after 3 seconds

      return () => clearTimeout(timer); // Cleanup timer on unmount or when isExpanding changes
    }
  }, [isExpanding]);

  // Thermal detection logic is handled in the satelliteData processing useEffect
  // No need for test logs on mount

  // Parse satellite data from chat responses
  useEffect(() => {
    if (!lastChatResponse) {
      return; // Skip silently when no response
    }

    try {
      console.log('MapView: ====== PROCESSING CHAT RESPONSE ======');
      console.log('MapView: Full response object:', lastChatResponse);
      console.log('MapView: Response type:', typeof lastChatResponse);
      console.log('MapView: Response keys:', Object.keys(lastChatResponse || {}));
      console.log('MapView: JSON stringified response:', JSON.stringify(lastChatResponse, null, 2));

      // CHECK: Determine if this is a new STAC query or just a vision/chat response
      // Vision responses are plain strings or objects without new STAC data
      // We should NOT reset satellite data for vision responses - they need the existing data for analysis!
      const hasNewStacData =
        lastChatResponse?.data?.stac_results?.features?.length > 0 ||
        lastChatResponse?.translation_metadata?.stac_query?.collections?.length > 0 ||
        lastChatResponse?.action === 'navigate_to';
      const isPlainTextResponse = typeof lastChatResponse === 'string';

      // Only reset map state when there's actual NEW STAC data to replace it with
      if (hasNewStacData) {
        // CRITICAL FIX: Reset ALL map state when a new STAC query arrives
        // This prevents the map from using stale data from a previous query
        // (e.g., Australia bounds when switching to Greece query)
        setOriginalBounds(null);
        setSatelliteData(null); // Clear old satellite data immediately
        setLastCollection(null); // Clear collection tracking
        console.log(
          '[SYNC] MapView: Reset all map state (originalBounds, satelliteData, lastCollection) for new STAC query'
        );
      } else if (isPlainTextResponse) {
        // Vision/chat responses - preserve existing satellite data
        console.log(
          '[MSG] MapView: Plain text response (vision/chat) - preserving existing satellite data'
        );
        return; // Don't process further - this is just a text response
      } else {
        console.log('ℹ️ MapView: No new STAC data detected - preserving existing satellite data');
      }

      // HANDLE NAVIGATE_TO ACTION: Pan map to location without loading STAC tiles
      const isNavigateToAction =
        lastChatResponse.action === 'navigate_to' && lastChatResponse.navigate_to;
      if (isNavigateToAction) {
        console.log('MapView: [PLANE] NAVIGATE_TO action detected - panning to location');
        console.log('MapView: navigate_to data:', lastChatResponse.navigate_to);

        const navigateToData = lastChatResponse.navigate_to;

        if (
          navigateToData.bbox &&
          Array.isArray(navigateToData.bbox) &&
          navigateToData.bbox.length === 4
        ) {
          console.log('MapView: Using bbox for navigation:', navigateToData.bbox);

          // Use Leaflet to fit bounds
          if (map && typeof map.fitBounds === 'function') {
            const [minLon, minLat, maxLon, maxLat] = navigateToData.bbox;
            map.fitBounds(
              [
                [minLat, minLon],
                [maxLat, maxLon],
              ],
              { padding: [50, 50] }
            );
            console.log('MapView: Leaflet fitBounds to bbox:', navigateToData.bbox);
          } else {
            console.warn('MapView: Map not ready for camera update');
          }
        } else if (navigateToData.latitude && navigateToData.longitude) {
          console.log(
            'MapView: Using lat/lon for navigation:',
            navigateToData.latitude,
            navigateToData.longitude
          );

          // Fallback to center + zoom if no bbox
          if (map && typeof map.setView === 'function') {
            map.setView(
              [navigateToData.latitude, navigateToData.longitude],
              navigateToData.zoom || 10
            );
            console.log('MapView: Leaflet setView to center:', [
              navigateToData.latitude,
              navigateToData.longitude,
            ]);
          } else {
            console.warn('MapView: Map not ready for camera update');
          }
        }

        // For navigate_to, we're done - don't process STAC data
        console.log('MapView: navigate_to complete - no STAC tiles to load');

        // AUTO-UPDATE PIN: If extreme_weather module is active and user navigates
        // to a new location, auto-set the pin to the navigation center so follow-up
        // climate questions use the correct coordinates without re-dropping a pin.
        if (
          selectedModule === 'extreme_weather' &&
          navigateToData.latitude &&
          navigateToData.longitude
        ) {
          console.log(
            'MapView: Auto-updating extreme weather pin to navigation center:',
            navigateToData.latitude,
            navigateToData.longitude
          );
          setPinState((prev) => ({
            ...prev,
            lat: navigateToData.latitude,
            lng: navigateToData.longitude,
            active: true,
          }));
          // Notify parent of new pin coordinates
          if (onPinChange) {
            onPinChange({ lat: navigateToData.latitude, lng: navigateToData.longitude });
          }
          // Show "ready" message for the new location
          if (onGeointAnalysis) {
            const locationName = navigateToData.location_name || 'this location';
            onGeointAnalysis({
              type: 'extreme_weather_ready',
              message: '**Pin Placed**\n\nWhat would you like to know about this location?',
              coordinates: { lat: navigateToData.latitude, lng: navigateToData.longitude },
            });
          }
        }

        return;
      }

      // Add specific debugging for data structure
      if (lastChatResponse.data) {
        console.log('??? MapView: ? Found data object');
        console.log('??? MapView: Data keys:', Object.keys(lastChatResponse.data));
        console.log('??? MapView: Full data object:', lastChatResponse.data);
      }

      // Check for translation metadata containing original query
      if (lastChatResponse.translation_metadata) {
        console.log('??? MapView: ? Found translation_metadata');
        console.log('??? MapView: Translation metadata:', lastChatResponse.translation_metadata);
      }

      if (lastChatResponse.data) {
        if (lastChatResponse.data.stac_results) {
          console.log('??? MapView: ? Found stac_results');
          console.log('??? MapView: STAC results type:', typeof lastChatResponse.data.stac_results);
          console.log(
            '??? MapView: STAC results keys:',
            Object.keys(lastChatResponse.data.stac_results)
          );
          console.log('??? MapView: Full STAC results:', lastChatResponse.data.stac_results);

          // Check for features directly (correct structure)
          if (
            lastChatResponse.data.stac_results.features &&
            Array.isArray(lastChatResponse.data.stac_results.features)
          ) {
            console.log('??? MapView: ? Found features in stac_results');
            console.log(
              '??? MapView: Features count:',
              lastChatResponse.data.stac_results.features.length
            );
            console.log(
              '??? MapView: First feature:',
              lastChatResponse.data.stac_results.features[0]
            );

            // Process the STAC features for satellite data
            const stacFeatures = lastChatResponse.data.stac_results.features;
            if (stacFeatures.length > 0) {
              const firstFeature = stacFeatures[0];

              // CRITICAL FIX: Use the query bbox from translation_metadata, NOT the first tile's bbox
              // The query bbox represents the full geographic extent requested (e.g., all of Greece)
              // The first tile's bbox is just one small tile (~1° x 1°) which causes:
              // 1. Map to zoom in too much on initial load
              // 2. Incomplete spatial coverage (user sees only part of Greece)
              // 3. Tile expansion triggering incorrectly (originalBounds too small)
              const queryBbox = lastChatResponse.translation_metadata?.stac_query?.bbox;
              const bbox = queryBbox || firstFeature.bbox;

              if (queryBbox) {
                console.log('MapView: Using query bbox from translation_metadata:', queryBbox);
              } else {
                console.warn(
                  'MapView: No query bbox in translation_metadata, falling back to first feature bbox:',
                  firstFeature.bbox
                );
              }

              // Extract collection from the feature early for mosaic detection
              const collection =
                firstFeature.collection ||
                firstFeature.links
                  ?.find((link: any) => link.rel === 'collection')
                  ?.href?.split('/')
                  .pop();

              // ========================================================================
              // MOSAIC TILEJSON: Use MPC's mosaic service for seamless composited tiles
              // ========================================================================
              // The mosaic service automatically composites tiles from multiple dates,
              // solving the coverage gap problem for large areas (e.g., Greece HLS query)
              // where a single date's imagery doesn't cover the entire region.
              // ========================================================================
              const mosaicTilejson = lastChatResponse.translation_metadata?.mosaic_tilejson;

              if (mosaicTilejson && mosaicTilejson.tilejson_url) {
                console.log('MapView: MOSAIC TILEJSON DETECTED - Using seamless composited tiles!');
                console.log(`MapView: Mosaic search_id: ${mosaicTilejson.search_id}`);
                console.log(`MapView: Mosaic collection: ${mosaicTilejson.collection}`);
                console.log(
                  `MapView: Mosaic tilejson URL: ${mosaicTilejson.tilejson_url.substring(0, 150)}...`
                );

                // Store mosaic data - use single tilejson URL for the entire area
                // CRITICAL: Include full STAC items with assets for vision agent NDVI computation
                const mosaicSatelliteData: SatelliteData = {
                  bbox: bbox,
                  tile_url: mosaicTilejson.tilejson_url,
                  items: stacFeatures.slice(0, 10).map((feature: any) => ({
                    id: feature.id,
                    collection: feature.collection,
                    datetime: feature.properties?.datetime || new Date().toISOString(),
                    bbox: feature.bbox,
                    // Include assets with band URLs and type for vision agent raster analysis (NDVI, etc.)
                    assets: feature.assets
                      ? Object.fromEntries(
                          Object.entries(feature.assets).map(([key, value]: [string, any]) => [
                            key,
                            {
                              href: value?.href,
                              type: value?.type, // Include media type for raster detection
                            },
                          ])
                        )
                      : undefined,
                  })),
                  // Mark as mosaic for special handling in rendering
                  is_mosaic: true,
                  mosaic_search_id: mosaicTilejson.search_id,
                };

                setSatelliteData(mosaicSatelliteData);
                console.log('MapView: Set mosaic satellite data - single seamless tile layer');

                // Update map view to show entire coverage area
                if (map && bbox && typeof map.fitBounds === 'function') {
                  const [minLon, minLat, maxLon, maxLat] = bbox;
                  map.fitBounds(
                    [
                      [minLat, minLon],
                      [maxLat, maxLon],
                    ],
                    { padding: [50, 50] }
                  );
                }

                return; // Exit early - mosaic rendering will use single tilejson URL
              }

              // MULTI-TILE DEM RENDERING: Check if backend provided all_tile_urls
              const allTileUrls = lastChatResponse.translation_metadata?.all_tile_urls;

              // Fix incorrect assets in tile URLs from backend
              const fixTileUrlAssets = (url: string, collection: string): string => {
                if (
                  collection === 'sentinel-2-l2a' &&
                  url.includes('assets=red&assets=green&assets=blue')
                ) {
                  const fixedUrl = url.replace(
                    /assets=red&assets=green&assets=blue/g,
                    'assets=visual'
                  );
                  console.log(
                    'MapView: Fixed Sentinel-2 L2A tile URL assets from [red,green,blue] to [visual]'
                  );
                  return fixedUrl;
                }
                return url;
              };

              // Collections that need TileJSON resolution and should NOT go through the multi-tile path
              // They are handled by the useMosaicApproach path below (lines 691+)
              const mosaicApproachCollections = [
                'cop-dem-glo-30',
                'cop-dem-glo-90',
                'nasadem',
                '3dep-seamless',
                'alos-dem',
                'modis-09A1-061',
                'modis-09Q1-061',
                'modis-13Q1-061',
                'modis-13A1-061',
                'modis-15A2H-061',
                'modis-17A2H-061',
                'modis-11A2-061',
                'modis-64A1-061',
                'modis-14A1-061',
                'modis-14A2-061',
              ];
              const collectionNeedsMosaicApproach = mosaicApproachCollections.some((col) =>
                collection?.includes(col)
              );

              if (
                allTileUrls &&
                Array.isArray(allTileUrls) &&
                allTileUrls.length > 1 &&
                !collectionNeedsMosaicApproach
              ) {
                console.log('MapView: MULTI-TILE DEM DETECTED!');
                console.log(
                  `MapView: Backend provided ${allTileUrls.length} tile URLs for seamless coverage`
                );

                // CRITICAL FIX: Limit tiles to prevent overwhelming the tile server
                const MAX_TILES_TO_RENDER = 50;
                const shouldLimitTiles = allTileUrls.length > MAX_TILES_TO_RENDER;
                const tilesToRender = shouldLimitTiles
                  ? allTileUrls.slice(0, MAX_TILES_TO_RENDER)
                  : allTileUrls;

                if (shouldLimitTiles) {
                  console.warn(
                    `MapView: Found ${allTileUrls.length} tiles, limiting to ${MAX_TILES_TO_RENDER} for performance`
                  );
                }

                console.log('MapView: Rendering tile URLs:', tilesToRender);

                // Fix tile URLs with incorrect assets
                const fixedTileUrls = tilesToRender.map((tileUrlData: any) => ({
                  ...tileUrlData,
                  tilejson_url: fixTileUrlAssets(tileUrlData.tilejson_url, collection || 'unknown'),
                }));

                // Store all tile URLs in satellite data (limit items to match rendered tiles)
                const tilesToRenderFeatures = shouldLimitTiles
                  ? stacFeatures.slice(0, MAX_TILES_TO_RENDER)
                  : stacFeatures;
                const multiTileSatelliteData: SatelliteData = {
                  bbox: bbox,
                  tile_url: fixedTileUrls[0].tilejson_url, // Primary tile for backward compatibility
                  items: tilesToRenderFeatures.map((feature: any) => ({
                    id: feature.id,
                    collection: feature.collection,
                    datetime: feature.properties?.datetime || new Date().toISOString(),
                    bbox: feature.bbox,
                    // Include assets with band URLs and type for vision agent raster analysis
                    assets: feature.assets
                      ? Object.fromEntries(
                          Object.entries(feature.assets).map(([key, value]: [string, any]) => [
                            key,
                            {
                              href: value?.href,
                              type: value?.type, // Include media type for raster detection
                            },
                          ])
                        )
                      : undefined,
                  })),
                  all_tile_urls: fixedTileUrls, // Add multi-tile array with fixed URLs
                };

                setSatelliteData(multiTileSatelliteData);
                console.log('? MapView: Set multi-tile satellite data');

                // Update map view to show entire coverage area
                if (map && bbox && typeof map.fitBounds === 'function') {
                  const [minLon, minLat, maxLon, maxLat] = bbox;
                  map.fitBounds(
                    [
                      [minLat, minLon],
                      [maxLat, maxLon],
                    ],
                    { padding: [50, 50] }
                  );
                }

                return; // Exit early - multi-tile rendering will be handled in the rendering effect
              }

              // ??? MOSAIC APPROACH: Use continuous tile rendering for collections designed for seamless coverage

              // 1. Elevation/DEM Collections - Static terrain data
              const elevationCollections = [
                'cop-dem-glo-30',
                'cop-dem-glo-90',
                'nasadem',
                '3dep-seamless',
                'alos-dem',
              ];

              // 2. MODIS Composite Collections - Designed for global seamless coverage
              const modisCompositeCollections = [
                'modis-09A1-061',
                'modis-09Q1-061', // Surface reflectance composites
                'modis-13Q1-061',
                'modis-13A1-061', // Vegetation indices (NDVI/EVI)
                'modis-15A2H-061',
                'modis-17A2H-061', // LAI and GPP
                'modis-11A2-061', // Land surface temperature
                'modis-64A1-061', // Burned area
              ];

              // 3. MODIS Fire Collections - Global fire monitoring
              const modisFireCollections = ['modis-14A1-061', 'modis-14A2-061'];

              const isElevationCollection = elevationCollections.some((col) =>
                collection?.includes(col)
              );
              const isMODISComposite = modisCompositeCollections.some((col) =>
                collection?.includes(col)
              );
              const isMODISFire = modisFireCollections.some((col) => collection?.includes(col));
              const useMosaicApproach = isElevationCollection || isMODISComposite || isMODISFire;

              if (useMosaicApproach) {
                console.log('[PIN] MapView: MOSAIC COLLECTION DETECTED - Using TileJSON approach');
                console.log('MapView: Collection:', collection);
                console.log(
                  'MapView: Type:',
                  isElevationCollection
                    ? 'Elevation/DEM'
                    : isMODISFire
                      ? 'Fire Detection'
                      : 'MODIS Composite'
                );
                console.log('MapView: Number of STAC items:', stacFeatures.length);

                // CRITICAL FIX: Limit tiles to prevent overwhelming the tile server
                const MAX_TILES_TO_RENDER = 50;
                const shouldLimitMosaicTiles = stacFeatures.length > MAX_TILES_TO_RENDER;
                const mosaicTilesToRender = shouldLimitMosaicTiles
                  ? stacFeatures.slice(0, MAX_TILES_TO_RENDER)
                  : stacFeatures;

                if (shouldLimitMosaicTiles) {
                  console.warn(
                    `MapView: Found ${stacFeatures.length} mosaic tiles, limiting to ${MAX_TILES_TO_RENDER} for performance`
                  );
                }

                // Use the first feature's tilejson asset (CORRECT approach for MPC)
                const firstFeatureAssets = firstFeature.assets;
                let tileJsonUrl = '';

                if (firstFeatureAssets && firstFeatureAssets.tilejson) {
                  // Use the pre-built tilejson URL from the STAC item
                  tileJsonUrl = firstFeatureAssets.tilejson.href;
                  console.log('? MapView: Found tilejson asset in STAC item');
                  console.log('?? MapView: TileJSON URL:', tileJsonUrl);
                } else {
                  // Fallback: Build tilejson URL manually
                  const itemId = firstFeature.id;
                  if (isElevationCollection) {
                    tileJsonUrl = `https://planetarycomputer.microsoft.com/api/data/v1/item/tilejson.json?collection=${collection}&item=${itemId}&assets=data&colormap_name=terrain&rescale=0,4000&format=png`;
                    console.log('?? MapView: Built elevation tilejson URL (fallback)');
                  } else {
                    console.warn(
                      '?? MapView: No tilejson asset found and not elevation collection'
                    );
                    return; // Can't proceed without tilejson
                  }
                }

                // Fetch and use TileJSON (using IIFE for async)
                (async () => {
                  try {
                    console.log(
                      'MapView: Fetching TileJSON from:',
                      tileJsonUrl.substr(0, 100) + '...'
                    );

                    // Sign the tilejson URL
                    let signedTileJsonUrl = tileJsonUrl;
                    try {
                      // CRITICAL: Use API_BASE_URL to ensure request goes to correct backend
                      const signResponse = await authenticatedFetch(
                        `${API_BASE_URL}/api/sign-mosaic-url`,
                        {
                          method: 'POST',
                          headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify({ url: tileJsonUrl }),
                        }
                      );

                      if (signResponse.ok) {
                        const signData = await signResponse.json();
                        signedTileJsonUrl = signData.signed_url;
                        console.log('[LOCK] MapView: Signed TileJSON URL');
                      }
                    } catch (signError) {
                      console.warn('MapView: Could not sign TileJSON URL, using unsigned');
                    }

                    // Fetch the TileJSON
                    const tilejsonResponse = await fetch(signedTileJsonUrl);
                    if (!tilejsonResponse.ok) {
                      throw new Error(`TileJSON fetch failed: ${tilejsonResponse.status}`);
                    }

                    const tilejsonData = await tilejsonResponse.json();
                    console.log('? MapView: TileJSON loaded successfully');
                    console.log('?? MapView: TileJSON bounds:', tilejsonData.bounds);
                    console.log(
                      '?? MapView: Tile URL template:',
                      tilejsonData.tiles[0].substr(0, 100) + '...'
                    );

                    // Use the tile URL template from TileJSON
                    const tileUrlTemplate = tilejsonData.tiles[0];

                    // CRITICAL FIX: Prioritize backend-resolved bbox over STAC feature bboxes
                    // For global elevation datasets (cop-dem-glo-90), STAC features can have random tile bboxes
                    let overallBbox = bbox; // Start with query-resolved bbox from backend

                    // Check if backend provided location-specific bounds in the response metadata
                    if (lastChatResponse?.data?.search_metadata?.spatial_extent) {
                      const spatialExtent = lastChatResponse.data.search_metadata.spatial_extent;
                      if (Array.isArray(spatialExtent) && spatialExtent.length === 4) {
                        overallBbox = spatialExtent;
                        console.log(
                          'MapView: Using backend-resolved spatial_extent for map bounds:',
                          overallBbox
                        );
                      }
                    } else {
                      console.log(
                        'MapView: No spatial_extent from backend, using original bbox:',
                        bbox
                      );
                    }

                    // Only calculate from STAC features as a last resort fallback
                    if (!overallBbox || overallBbox.some((coord: number) => !isFinite(coord))) {
                      console.log('MapView: Fallback - calculating bbox from STAC features');
                      let minLng = Infinity,
                        minLat = Infinity,
                        maxLng = -Infinity,
                        maxLat = -Infinity;
                      mosaicTilesToRender.forEach((feature: any) => {
                        if (feature.bbox && feature.bbox.length >= 4) {
                          const [west, south, east, north] = feature.bbox;
                          if (
                            west !== null &&
                            !isNaN(west) &&
                            isFinite(west) &&
                            south !== null &&
                            !isNaN(south) &&
                            isFinite(south) &&
                            east !== null &&
                            !isNaN(east) &&
                            isFinite(east) &&
                            north !== null &&
                            !isNaN(north) &&
                            isFinite(north)
                          ) {
                            minLng = Math.min(minLng, west);
                            minLat = Math.min(minLat, south);
                            maxLng = Math.max(maxLng, east);
                            maxLat = Math.max(maxLat, north);
                          }
                        }
                      });

                      if (minLng !== Infinity) {
                        overallBbox = [minLng, minLat, maxLng, maxLat];
                        console.log('MapView: Calculated bbox from STAC features:', overallBbox);
                      }
                    }

                    console.log('MapView: Final bbox for elevation display:', overallBbox);

                    const elevationData: SatelliteData = {
                      bbox: overallBbox,
                      tile_url: tileUrlTemplate, // Use the correct tile URL from TileJSON
                      items: mosaicTilesToRender.map((feature: any) => ({
                        id: feature.id,
                        collection: feature.collection,
                        datetime: feature.properties?.datetime || new Date().toISOString(),
                        bbox: feature.bbox,
                      })),
                    };

                    setSatelliteData(elevationData);
                    console.log('? MapView: Set elevation data with TileJSON tiles');
                    console.log(
                      '?? MapView: Tiles will now render from authenticated TileJSON endpoint'
                    );

                    if (map && overallBbox && typeof map.fitBounds === 'function') {
                      const [minLon, minLat, maxLon, maxLat] = overallBbox;
                      map.fitBounds(
                        [
                          [minLat, minLon],
                          [maxLat, maxLon],
                        ],
                        { padding: [50, 50] }
                      );
                    }
                  } catch (error) {
                    console.error('? MapView: Error fetching TileJSON:', error);
                    console.warn('?? MapView: Falling back to individual item rendering');
                    // Fall through to normal processing below
                  }
                })();

                // Early return since mosaic handling is done asynchronously above

                return;
              }

              // Try to get a tile server URL from assets in order of preference
              let tileUrl: string | null = null;
              if (firstFeature.assets) {
                console.log(
                  '??? MapView: [DEBUG] Assets available:',
                  Object.keys(firstFeature.assets)
                );
                console.log(
                  '??? MapView: [DEBUG] Tilejson asset exists:',
                  !!firstFeature.assets.tilejson
                );
                if (firstFeature.assets.tilejson) {
                  console.log('??? MapView: [DEBUG] Tilejson asset:', firstFeature.assets.tilejson);
                }

                // Priority 1: Check if backend provided optimized tile URLs
                // Backend uses HybridRenderingSystem for 113+ collections with optimal parameters
                const backendOptimizedUrls = lastChatResponse.translation_metadata?.all_tile_urls;
                const backendOptimizedUrl = backendOptimizedUrls?.find(
                  (urlData: any) => urlData.item_id === firstFeature.id
                );

                let tilejsonUrl: string | null = null;

                if (backendOptimizedUrl && backendOptimizedUrl.tilejson_url) {
                  // ? BEST: Use backend-optimized URL with HybridRenderingSystem parameters
                  tilejsonUrl = backendOptimizedUrl.tilejson_url;
                  console.log(
                    '? MapView: Using backend-optimized tile URL from HybridRenderingSystem'
                  );
                  console.log('?? MapView: Optimized URL:', tilejsonUrl);
                } else if (firstFeature.assets.tilejson) {
                  // ?? FALLBACK: Use STAC tilejson URL (may lack optimization)
                  console.log(
                    '?? MapView: Backend optimization not available, using STAC tilejson URL'
                  );
                  let stacTilejsonUrl = firstFeature.assets.tilejson.href;
                  console.log('??? MapView: Original STAC Tilejson URL:', stacTilejsonUrl);

                  // Fix incorrect assets in tilejson URL
                  stacTilejsonUrl = fixTileUrlAssets(stacTilejsonUrl, collection || 'unknown');
                  console.log('??? MapView: Fixed Tilejson URL:', stacTilejsonUrl);
                  console.log('??? MapView: [DEBUG] Feature collection:', collection);

                  // ===== LEGACY HLS RESCALE FIX =====
                  // Only needed if backend doesn't provide optimized URL
                  // HLS imagery requires rescale=(0,3000) to display properly
                  const isHLSCollection = collection === 'hls2-s30' || collection === 'hls2-l30';
                  if (isHLSCollection) {
                    console.log(
                      '?? MapView: [LEGACY FIX] HLS COLLECTION - adding rescale parameter'
                    );
                    const urlParts = stacTilejsonUrl.split('?');
                    if (urlParts.length === 2) {
                      const params = new URLSearchParams(urlParts[1]);
                      if (!params.has('rescale')) {
                        params.set('rescale', '0,3000');
                        stacTilejsonUrl = `${urlParts[0]}?${params.toString()}`;
                        console.log('?? MapView: Added rescale=0,3000 for HLS imagery');
                      }
                    }
                  }

                  tilejsonUrl = stacTilejsonUrl;

                  // THERMAL/FIRE DETECTION: Modify tilejson URL for thermal and wildfire queries
                  const isLandsatCollection = collection === 'landsat-c2-l2';
                  const isMODISFireCollection =
                    collection &&
                    (collection.includes('modis-14A1') ||
                      collection.includes('modis-14A2') ||
                      collection.includes('modis-64A1'));
                  const originalQuery = lastChatResponse.translation_metadata?.original_query || '';
                  const isThermalQuery =
                    originalQuery.toLowerCase().includes('thermal') ||
                    originalQuery.toLowerCase().includes('infrared') ||
                    originalQuery.toLowerCase().includes('heat') ||
                    originalQuery.toLowerCase().includes('temperature');
                  const isFireQuery =
                    originalQuery.toLowerCase().includes('fire') ||
                    originalQuery.toLowerCase().includes('wildfire') ||
                    originalQuery.toLowerCase().includes('burn');

                  // Enhanced debugging for thermal/fire detection
                  console.log('?? MapView: [THERMAL/FIRE DEBUG] Collection check:', {
                    collection: collection,
                    isLandsatCollection: isLandsatCollection,
                    isMODISFireCollection: isMODISFireCollection,
                    originalQuery: originalQuery,
                    isThermalQuery: isThermalQuery,
                    isFireQuery: isFireQuery,
                    thermal_found: originalQuery.toLowerCase().includes('thermal'),
                    infrared_found: originalQuery.toLowerCase().includes('infrared'),
                    heat_found: originalQuery.toLowerCase().includes('heat'),
                    temperature_found: originalQuery.toLowerCase().includes('temperature'),
                    fire_found: originalQuery.toLowerCase().includes('fire'),
                    wildfire_found: originalQuery.toLowerCase().includes('wildfire'),
                    burn_found: originalQuery.toLowerCase().includes('burn'),
                  });

                  if (
                    (isLandsatCollection && isThermalQuery) ||
                    (isMODISFireCollection && isFireQuery)
                  ) {
                    if (isLandsatCollection) {
                      console.log(
                        '?? MapView: THERMAL QUERY DETECTED for Landsat - switching to thermal infrared bands'
                      );
                    } else if (isMODISFireCollection) {
                      console.log(
                        '?? MapView: WILDFIRE QUERY DETECTED for MODIS - switching to fire visualization'
                      );
                    }
                    console.log('?? MapView: Original query:', originalQuery);

                    // Handle Landsat thermal data
                    if (isLandsatCollection && isThermalQuery) {
                      // Check if thermal assets are available
                      const thermalAssets = ['lwir11', 'lwir', 'thermal'];
                      const availableThermalAsset = thermalAssets.find(
                        (asset) => firstFeature.assets[asset]
                      );

                      if (availableThermalAsset && tilejsonUrl) {
                        console.log(
                          '?? MapView: ? THERMAL MODE ACTIVATED! Asset:',
                          availableThermalAsset
                        );

                        // Set thermal mode state
                        setIsThermalMode(true);

                        // Modify the tilejson URL to use thermal band instead of RGB
                        // Apply thermal visualization with adaptive rescale ranges for better contrast
                        // Using 'plasma' colormap: dark purple (cool) to bright yellow (hot)
                        const thermalRanges = [
                          '270,330', // Default balanced range
                          '250,350', // Wider range for more variation
                          '230,280', // Cooler range for summer scenes
                          '290,340', // Warmer range for winter scenes
                        ];

                        // Use default range for now, but log alternatives for debugging
                        const selectedRange = thermalRanges[0];
                        tilejsonUrl = tilejsonUrl
                          .replace(
                            'assets=red&assets=green&assets=blue',
                            `assets=${availableThermalAsset}`
                          )
                          .replace(
                            'color_formula=gamma+RGB+2.7%2C+saturation+1.5%2C+sigmoidal+RGB+15+0.55',
                            `rescale=${selectedRange}&colormap_name=plasma`
                          );
                      }
                    }
                    // Handle MODIS fire data
                    else if (isMODISFireCollection && isFireQuery && tilejsonUrl) {
                      console.log('?? MapView: ? FIRE MODE ACTIVATED for MODIS!');

                      // Set thermal mode state for fire visualization
                      setIsThermalMode(true);

                      // MODIS fire collections have different asset structure
                      // Priority order: FireMask (best for fire visualization), MaxFRP, QA
                      // Note: rendered_preview causes 404 tile errors, so we use FireMask directly
                      const fireAssets = ['FireMask', 'MaxFRP', 'QA'];
                      let availableFireAsset = null;

                      // Check what assets are available
                      console.log(
                        '?? MapView: [MODIS] Available assets:',
                        firstFeature.assets ? Object.keys(firstFeature.assets) : 'None'
                      );

                      if (firstFeature.assets) {
                        availableFireAsset = fireAssets.find((asset) => firstFeature.assets[asset]);

                        console.log('?? MapView: [MODIS] Selected fire asset:', availableFireAsset);

                        if (availableFireAsset) {
                          // Clean up the tilejson URL first
                          let baseUrl = tilejsonUrl.split('?')[0];
                          let params = new URLSearchParams(tilejsonUrl.split('?')[1] || '');

                          // Remove ALL RGB-related parameters that conflict with fire assets
                          params.delete('color_formula');
                          params.delete('expression');
                          params.delete('bidx');
                          params.delete('color_map');
                          params.delete('colormap');
                          params.delete('nodata');
                          params.delete('unscale');
                          params.delete('resampling');
                          params.delete('return_mask');

                          // Set the fire asset
                          params.set('assets', availableFireAsset);

                          // Apply appropriate visualization based on asset type
                          if (availableFireAsset === 'FireMask') {
                            // FireMask: Use MODIS fire colormap for fire confidence (matches planetary computer default)
                            params.set('colormap_name', 'modis-14A1|A2');
                            params.set('format', 'png');
                            console.log(
                              '?? MapView: [MODIS] Using FireMask with MODIS fire colormap for fire confidence'
                            );
                          } else if (availableFireAsset === 'MaxFRP') {
                            // MaxFRP: Fire radiative power - use hot colormap
                            params.set('colormap_name', 'viridis');
                            params.set('rescale', '0,500'); // Fire radiative power in MW
                            params.set('format', 'png');
                            console.log(
                              '?? MapView: [MODIS] Using MaxFRP with viridis colormap for fire intensity'
                            );
                          } else {
                            // Fallback for other assets (QA, etc.)
                            params.set('colormap_name', 'plasma');
                            params.set('format', 'png');
                            console.log('?? MapView: [MODIS] Using fallback plasma colormap');
                          }

                          // Reconstruct URL with clean parameters
                          tilejsonUrl = baseUrl + '?' + params.toString();
                        }
                      }

                      console.log(
                        '?? MapView: [MODIS] Modified fire visualization URL:',
                        tilejsonUrl
                      );
                    } else {
                      console.log(
                        '?? MapView: No thermal assets found, checking available assets:',
                        Object.keys(firstFeature.assets)
                      );
                    }
                  } else {
                    console.log(
                      '?? MapView: Thermal mode not detected - using standard RGB processing'
                    );
                    setIsThermalMode(false);
                  }

                  console.log('??? MapView: Final Tilejson URL:', tilejsonUrl);

                  // Process tilejson asynchronously with collection info for authentication
                  if (tilejsonUrl) {
                    fetchAndSignTileJSON(tilejsonUrl, { collection })
                      .then((result) => {
                        if (result.success && result.tileTemplate) {
                          console.log(
                            '??? MapView: [DEBUG] Processed tile URL:',
                            result.tileTemplate
                          );

                          if (bbox && result.tileTemplate) {
                            console.log(
                              '??? MapView: Creating satellite data from STAC feature with tilejson'
                            );
                            console.log('??? MapView: BBOX:', bbox);
                            console.log('??? MapView: Tile URL:', result.tileTemplate);
                            console.log(
                              '??? MapView: Tile URL type:',
                              result.tileTemplate.includes('{z}') ? 'Tile template' : 'Static image'
                            );

                            setSatelliteData({
                              bbox: bbox,
                              tile_url: result.tileTemplate,
                              items: stacFeatures.slice(0, 5).map((feature: any) => ({
                                id: feature.id,
                                collection: feature.collection,
                                datetime: feature.properties?.datetime || new Date().toISOString(),
                                bbox: feature.bbox,
                                // Include assets with band URLs for vision agent raster analysis
                                assets: feature.assets
                                  ? Object.fromEntries(
                                      Object.entries(feature.assets).map(
                                        ([key, value]: [string, any]) => [
                                          key,
                                          { href: value?.href },
                                        ]
                                      )
                                    )
                                  : undefined,
                              })),
                              thermal_mode: isThermalMode,
                              thermal_timestamp: isThermalMode ? Date.now() : undefined, // Force refresh for thermal
                            });
                          }
                        }
                      })
                      .catch((error: any) => {
                        console.log(
                          '??? MapView: [ERROR] Failed to process tilejson, using fallback:',
                          error
                        );
                        // Continue with fallback processing below
                      });

                    // Early return to avoid duplicate processing
                    return;
                  } else {
                    console.log('?? MapView: No tilejsonUrl available after optimization check');
                  }
                }
                // Priority 2: Use rendered_preview for static preview (fallback for static images)
                else if (firstFeature.assets.rendered_preview) {
                  console.log('??? MapView: Using rendered_preview asset URL (static image)');
                  tileUrl = firstFeature.assets.rendered_preview.href;
                }
                // Priority 3: Fallback to visual asset (direct TIFF - convert to preview)
                else if (firstFeature.assets.visual) {
                  console.log('??? MapView: Converting visual asset to preview URL');
                  // Try to convert visual asset to a preview URL
                  const collection = firstFeature.collection;
                  const itemId = firstFeature.id;
                  if (collection && itemId) {
                    tileUrl = `https://planetarycomputer.microsoft.com/api/data/v1/item/preview.png?collection=${collection}&item=${itemId}&assets=visual&format=png`;
                    console.log('??? MapView: Generated preview URL from visual asset:', tileUrl);
                  } else {
                    tileUrl = firstFeature.assets.visual.href;
                    console.log(
                      '??? MapView: Using visual asset URL directly (may not work as tile):',
                      tileUrl
                    );
                  }
                }
              }

              // Fallback: look for preview links
              if (!tileUrl && firstFeature.links) {
                const previewLink = firstFeature.links.find((link: any) => link.rel === 'preview');
                if (previewLink) {
                  console.log('??? MapView: Using preview link from links array');
                  tileUrl = previewLink.href;
                }
              }

              if (bbox && tileUrl) {
                console.log('??? MapView: Creating satellite data from STAC feature');
                console.log('??? MapView: BBOX:', bbox);
                console.log('??? MapView: Tile URL:', tileUrl);
                console.log(
                  '??? MapView: Tile URL type:',
                  tileUrl.includes('{z}') ? 'Tile template' : 'Static image'
                );

                setSatelliteData({
                  bbox: bbox,
                  tile_url: tileUrl,
                  items: stacFeatures.slice(0, 5).map((feature: any) => ({
                    id: feature.id,
                    collection: feature.collection,
                    datetime: feature.properties?.datetime || new Date().toISOString(),
                    bbox: feature.bbox,
                    // Include assets with band URLs for vision agent raster analysis
                    assets: feature.assets
                      ? Object.fromEntries(
                          Object.entries(feature.assets).map(([key, value]: [string, any]) => [
                            key,
                            { href: value?.href },
                          ])
                        )
                      : undefined,
                  })),
                });
              }
            }
          } else if (lastChatResponse.data.stac_results.results) {
            // Fallback: check old structure for backwards compatibility
            console.log('??? MapView: ? Found results in stac_results (old structure)');
            console.log(
              '??? MapView: Results type:',
              typeof lastChatResponse.data.stac_results.results
            );
            console.log(
              '??? MapView: Results keys:',
              Object.keys(lastChatResponse.data.stac_results.results)
            );
            console.log(
              '??? MapView: Full results object:',
              lastChatResponse.data.stac_results.results
            );
          } else {
            console.log('??? MapView: ? No features or results in stac_results');
            console.log(
              '??? MapView: Available keys:',
              Object.keys(lastChatResponse.data.stac_results)
            );
          }
        } else {
          console.log('??? MapView: ? No stac_results in data');
        }
      } else {
        console.log('??? MapView: ? No data object in response');
      }

      // Only process STAC results - no hardcoded fallbacks allowed

      // Check for STAC results structure from the API
      if (lastChatResponse.data && lastChatResponse.data.stac_results) {
        console.log('??? MapView: Found STAC results structure');
        console.log('??? MapView: STAC results object:', lastChatResponse.data.stac_results);

        const stacResults = lastChatResponse.data.stac_results;

        // Handle multiple possible data structures
        let features = null;

        // Case 1: Direct features array (current API format)
        if (stacResults.features && Array.isArray(stacResults.features)) {
          features = stacResults.features;
          console.log(
            '??? MapView: ? Found direct features array with',
            features.length,
            'STAC features'
          );
        }
        // Case 2: results.features format (legacy)
        else if (
          stacResults.results &&
          stacResults.results.features &&
          Array.isArray(stacResults.results.features)
        ) {
          features = stacResults.results.features;
          console.log(
            '??? MapView: ? Found results.features array with',
            features.length,
            'STAC features'
          );
        }
        // Case 3: FeatureCollection format
        else if (
          stacResults.results &&
          stacResults.results.type === 'FeatureCollection' &&
          stacResults.results.features
        ) {
          features = stacResults.results.features;
          console.log(
            '??? MapView: ? Found FeatureCollection with',
            features.length,
            'STAC features'
          );
        }
        // Case 4: Direct results array
        else if (stacResults.results && Array.isArray(stacResults.results)) {
          features = stacResults.results;
          console.log(
            '??? MapView: ? Found direct results array with',
            features.length,
            'features'
          );
        }

        if (features && features.length > 0) {
          // CRITICAL FIX: Limit tiles to prevent overwhelming the tile server
          // When querying large areas (like entire countries), STAC can return 1000+ results
          // Loading all tiles simultaneously causes ERR_HTTP2_SERVER_REFUSED_STREAM errors
          const MAX_TILES_TO_RENDER = 50; // Reasonable limit for performance
          const shouldLimitTiles = features.length > MAX_TILES_TO_RENDER;

          if (shouldLimitTiles) {
            console.warn(
              `MapView: Found ${features.length} STAC items, limiting to ${MAX_TILES_TO_RENDER} for performance`
            );
            console.warn(
              `MapView: To see more tiles, zoom in or refine your query with date/cloud filters`
            );
          }

          const tilesToRender = shouldLimitTiles
            ? features.slice(0, MAX_TILES_TO_RENDER)
            : features;

          // CRITICAL FIX: Use the query bbox from translation_metadata, NOT the STAC feature bboxes
          // The query bbox represents the exact location the user requested (e.g., Washington DC)
          // STAC feature bboxes can be MUCH larger (Landsat tiles are ~185km x 185km each)
          // Using feature bboxes causes the map to center on the combined tile coverage, not the requested location
          let overallBbox: number[] | undefined = undefined;

          // Priority 1: Use query bbox from translation_metadata (backend-resolved location)
          const queryBbox = lastChatResponse.translation_metadata?.stac_query?.bbox;
          if (queryBbox && Array.isArray(queryBbox) && queryBbox.length >= 4) {
            const [west, south, east, north] = queryBbox;
            if (isFinite(west) && isFinite(south) && isFinite(east) && isFinite(north)) {
              overallBbox = queryBbox;
              console.log(
                'MapView: Using query bbox from translation_metadata (user-requested location):',
                overallBbox
              );
            }
          }

          // Priority 2: Fallback to calculating from STAC features (only if no query bbox)
          if (!overallBbox && features.length > 0) {
            console.warn(
              'MapView: No query bbox in translation_metadata, calculating from STAC features'
            );
            let minLng = Infinity,
              minLat = Infinity,
              maxLng = -Infinity,
              maxLat = -Infinity;

            features.forEach((feature: any) => {
              if (feature.bbox && feature.bbox.length >= 4) {
                const [west, south, east, north] = feature.bbox;

                // Validate each coordinate before using it
                if (
                  west !== null &&
                  !isNaN(west) &&
                  isFinite(west) &&
                  south !== null &&
                  !isNaN(south) &&
                  isFinite(south) &&
                  east !== null &&
                  !isNaN(east) &&
                  isFinite(east) &&
                  north !== null &&
                  !isNaN(north) &&
                  isFinite(north)
                ) {
                  minLng = Math.min(minLng, west);
                  minLat = Math.min(minLat, south);
                  maxLng = Math.max(maxLng, east);
                  maxLat = Math.max(maxLat, north);
                } else {
                  console.warn(
                    'MapView: Skipping feature with invalid bbox coordinates:',
                    feature.bbox
                  );
                }
              }
            });

            if (
              minLng !== Infinity &&
              isFinite(minLng) &&
              isFinite(minLat) &&
              isFinite(maxLng) &&
              isFinite(maxLat)
            ) {
              overallBbox = [minLng, minLat, maxLng, maxLat];
              console.log(
                'MapView: Calculated valid overall bbox from STAC features:',
                overallBbox
              );
            } else {
              console.warn('MapView: Could not calculate valid overall bbox from features');
            }
          }

          // ============================================================
          // BACKEND-ONLY TILE URL APPROACH (MPC Best Practice)
          // ============================================================
          // Backend HybridRenderingSystem generates ALL tile URLs with optimal parameters
          // Frontend ONLY uses backend-provided URLs - no fallback generation
          // This follows Microsoft Planetary Computer pattern where backend defines render configurations
          // ============================================================

          // Get backend-optimized tile URLs (includes rescale, color_formula, optimal bands, etc.)
          const backendOptimizedUrls = lastChatResponse.translation_metadata?.all_tile_urls;

          if (!backendOptimizedUrls || backendOptimizedUrls.length === 0) {
            console.error(
              'MapView: Backend did not provide optimized tile URLs. Cannot render tiles.'
            );
            console.error(
              'MapView: This indicates HybridRenderingSystem failed to process STAC results.'
            );
            console.error('MapView: STAC features:', features.length, 'items');
            // Don't try to generate URLs on frontend - backend is single source of truth
          }

          // Create satellite data structure using ONLY backend-provided URLs
          // Use tilesToRender (limited subset) instead of all features
          const newSatelliteData: SatelliteData = {
            bbox: overallBbox,
            items: tilesToRender.map((feature: any) => {
              const collection = feature.collection || 'unknown';
              const itemId = feature.id;

              // Find backend-optimized tile URL for this specific item
              const backendTileData = backendOptimizedUrls?.find(
                (urlData: any) => urlData.item_id === itemId
              );

              let tileUrl: string | null = null;
              let previewUrl: string | null = null;

              if (backendTileData?.tilejson_url) {
                tileUrl = backendTileData.tilejson_url;
                console.log(
                  `? MapView: Using backend-optimized tile URL for ${collection}:${itemId}`
                );
                console.log(
                  `?? MapView: Optimized URL: ${backendTileData.tilejson_url.substring(0, Math.min(150, backendTileData.tilejson_url.length))}...`
                );
              } else {
                console.warn(`?? MapView: No backend tile URL for ${collection}:${itemId}`);
                console.warn(
                  `?? MapView: This item will not be visualizable without backend optimization`
                );
                // Don't generate fallback - backend must provide URLs
              }

              // Find preview link from STAC (preview is less critical than tile URL)
              if (feature.links) {
                const previewLink = feature.links.find((link: any) => link.rel === 'preview');
                if (previewLink) {
                  previewUrl = previewLink.href;
                }
              }

              return {
                id: itemId,
                collection: collection,
                datetime: feature.properties?.datetime || new Date().toISOString(),
                preview: previewUrl,
                tile_url: tileUrl, // ONLY backend URL, never frontend-generated
                bbox: feature.bbox,
              };
            }),
            // Overall preview URL (optional, for thumbnail display)
            preview_url: (() => {
              if (features[0]?.links) {
                const previewLink = features[0].links.find((link: any) => link.rel === 'preview');
                if (previewLink) return previewLink.href;
              }
              return undefined;
            })(),
            // Overall tile URL - use first item's backend-optimized URL
            tile_url: (() => {
              const backendTileData = backendOptimizedUrls?.find(
                (urlData: any) => urlData.item_id === features[0]?.id
              );

              if (backendTileData?.tilejson_url) {
                console.log('? MapView: Using backend-optimized tile URL for primary rendering');
                console.log('?? MapView: URL:', backendTileData.tilejson_url);
                return backendTileData.tilejson_url;
              }

              console.error('? MapView: No backend-optimized tile URL available for primary item');
              console.error(
                '? MapView: Collection:',
                features[0]?.collection,
                'Item:',
                features[0]?.id
              );
              console.error('? MapView: Backend must provide tile URLs via HybridRenderingSystem');
              return undefined; // No fallback - backend is single source of truth
            })(),
          };

          setSatelliteData(newSatelliteData);
          console.log(
            '??? MapView: Set STAC satellite data for map visualization:',
            newSatelliteData
          );

          // Update map view if we have a bounding box
          if (map && overallBbox) {
            // Determine minimum zoom based on collection type
            // MODIS data (1km resolution) needs zoom 10+ to be visible
            const collection = tilesToRender[0]?.collection?.toLowerCase() || '';
            const isModisData = collection.includes('modis');
            const minZoom = isModisData ? 10 : undefined;

            if (isModisData) {
              console.log('MapView: MODIS data detected, enforcing minimum zoom level of 10');
            }

            updateMapView(overallBbox, minZoom);
          }

          return;
        }
      }

      // Legacy support: Check if this is a structured response with satellite data
      if (lastChatResponse.dataset_ids && lastChatResponse.bbox) {
        console.log('MapView: Found legacy structured satellite data response');

        // Use collection-aware tile generation for legacy responses
        const firstDatasetId = lastChatResponse.dataset_ids[0];
        const collectionId = firstDatasetId?.split(':')[0] || 'sentinel-2-l2a';
        const itemId = firstDatasetId?.split(':')[1];

        const newSatelliteData: SatelliteData = {
          bbox: [
            lastChatResponse.bbox.west,
            lastChatResponse.bbox.south,
            lastChatResponse.bbox.east,
            lastChatResponse.bbox.north,
          ],
          items: lastChatResponse.dataset_ids.map((id: string) => {
            const collection = id.split(':')[0] || 'unknown';
            const item = id.split(':')[1];

            // BACKEND-ONLY: Look for backend-optimized URL
            const backendOptimizedUrls = lastChatResponse.translation_metadata?.all_tile_urls;
            const backendTileData = backendOptimizedUrls?.find(
              (urlData: any) => urlData.item_id === item
            );

            if (!backendTileData?.tilejson_url) {
              console.error(`? MapView: No backend tile URL for legacy item ${collection}:${item}`);
              console.error(`? MapView: Backend must provide tile URLs via HybridRenderingSystem`);
            }

            // Find preview link from STAC if available (less critical)
            let previewUrl: string | null = null;
            // Preview URLs are optional - we won't generate fallbacks

            return {
              id,
              collection,
              datetime: lastChatResponse.date_range?.start_date || new Date().toISOString(),
              preview: previewUrl,
              tile_url: backendTileData?.tilejson_url || null, // ONLY backend URL
            };
          }),
          preview_url: undefined, // Preview is optional
          tile_url: (() => {
            // Use first item's backend-optimized URL
            const firstItemId = lastChatResponse.dataset_ids[0]?.split(':')[1];
            const backendOptimizedUrls = lastChatResponse.translation_metadata?.all_tile_urls;
            const backendTileData = backendOptimizedUrls?.find(
              (urlData: any) => urlData.item_id === firstItemId
            );

            if (!backendTileData?.tilejson_url) {
              console.error(`? MapView: No backend tile URL for legacy primary item`);
            }

            return backendTileData?.tilejson_url || undefined;
          })(),
        };

        setSatelliteData(newSatelliteData);
        console.log('MapView: Set satellite data for map visualization:', newSatelliteData);
        return;
      }

      // Fallback: Try to parse as text response
      if (typeof lastChatResponse === 'string') {
        // Look for URLs in the response that might be tile URLs or preview images
        const urlPattern = /https?:\/\/[^\s<>"]+/g;
        const urls = lastChatResponse.match(urlPattern) || [];

        // Look for tile URLs (typically contain /tiles/ or similar)
        const tileUrls = urls.filter(
          (url: string) =>
            url.includes('/tiles/') ||
            url.includes('/tile/') ||
            url.includes('/preview') ||
            url.includes('/crop')
        );

        // Look for bbox coordinates in the response
        const bboxPattern =
          /\[?(-?\d+\.?\d*),\s*(-?\d+\.?\d*),\s*(-?\d+\.?\d*),\s*(-?\d+\.?\d*)\]?/g;
        const bboxMatch = lastChatResponse.match(bboxPattern);

        if (tileUrls.length > 0 && bboxMatch) {
          const parsedBbox = bboxMatch[0].split(',').map((n: string) => {
            const cleaned = n.replace(/[\[\]]/g, '').trim();
            const parsed = parseFloat(cleaned);
            // Validate parsed number
            if (isNaN(parsed) || !isFinite(parsed)) {
              console.error(`? Invalid coordinate value: "${cleaned}" -> ${parsed}`);
              return null;
            }
            return parsed;
          });

          // Check if any coordinates failed to parse
          if (parsedBbox.includes(null) || parsedBbox.length !== 4) {
            console.error(
              '? Failed to parse valid bbox coordinates:',
              bboxMatch[0],
              '-> parsed:',
              parsedBbox
            );
          } else {
            const data: SatelliteData = {
              bbox: parsedBbox as number[],
              items: [],
              preview_url: tileUrls.find((url: string) => url.includes('/preview')) || tileUrls[0],
              tile_url:
                tileUrls.find((url: string) => url.includes('/tiles/')) ||
                tileUrls.find((url: string) => url.includes('/tile/')),
            };

            console.log('? Successfully parsed satellite data with bbox:', parsedBbox);
            setSatelliteData(data);
          }
        }
      }
    } catch (error) {
      console.error('Error parsing satellite data:', error);
    }
  }, [lastChatResponse, map]);

  // Add map update function for bounding box
  // minZoom parameter enforces a minimum zoom level (e.g., 10 for MODIS 1km data)
  const updateMapView = (bbox: number[] | null, minZoom?: number) => {
    if (map && bbox && bbox.length >= 4) {
      try {
        console.log('MapView: updateMapView called with bbox:', bbox, 'provider:', mapProvider);

        // Debug: Log the call stack to trace where updateMapView was called from
        console.trace('MapView: updateMapView call stack');

        const [west, south, east, north] = bbox;

        // Enhanced validation: Check for null/undefined values first
        if (
          west === null ||
          west === undefined ||
          south === null ||
          south === undefined ||
          east === null ||
          east === undefined ||
          north === null ||
          north === undefined
        ) {
          throw new Error(
            `Null coordinate values detected: west=${west}, south=${south}, east=${east}, north=${north}`
          );
        }

        // Check for NaN values
        if (isNaN(west) || isNaN(south) || isNaN(east) || isNaN(north)) {
          throw new Error(
            `NaN coordinate values detected: west=${west}, south=${south}, east=${east}, north=${north}`
          );
        }

        // Validate coordinate ranges (allow small tolerance for rounding errors at dateline)
        const DATELINE_TOLERANCE = 0.01; // ~1km tolerance at dateline
        if (
          west < -180 - DATELINE_TOLERANCE ||
          west > 180 + DATELINE_TOLERANCE ||
          east < -180 - DATELINE_TOLERANCE ||
          east > 180 + DATELINE_TOLERANCE
        ) {
          throw new Error(`Invalid longitude values: west=${west}, east=${east}`);
        }
        if (south < -90 || south > 90 || north < -90 || north > 90) {
          throw new Error(`Invalid latitude values: south=${south}, north=${north}`);
        }

        // CRITICAL: Clamp latitudes to WebMercator limits (±85.05°)
        // WebMercator projection which is undefined at ±90° latitude.
        // Passing bounds with lat outside ~±85.06° causes the SDK to produce null values
        // in internal style evaluation ("Expected value to be of type number, but found null").
        const WEB_MERCATOR_LAT_LIMIT = 85.05;
        const clampedSouth = Math.max(
          -WEB_MERCATOR_LAT_LIMIT,
          Math.min(WEB_MERCATOR_LAT_LIMIT, south)
        );
        const clampedNorth = Math.max(
          -WEB_MERCATOR_LAT_LIMIT,
          Math.min(WEB_MERCATOR_LAT_LIMIT, north)
        );
        const clampedWest = Math.max(-180, Math.min(180, west));
        const clampedEast = Math.max(-180, Math.min(180, east));

        if (clampedSouth !== south || clampedNorth !== north) {
          console.warn(
            `MapView: Clamped latitudes to WebMercator limits: south ${south}->${clampedSouth}, north ${north}->${clampedNorth}`
          );
        }

        // For dateline-crossing bounds, west > east is valid (e.g., 170 to -170 crosses dateline)
        // Only check if south >= north (always invalid)
        if (clampedSouth >= clampedNorth) {
          throw new Error(
            `Invalid bbox bounds after WebMercator clamping: south=${clampedSouth} >= north=${clampedNorth} (original: south=${south}, north=${north})`
          );
        }

        // Warn but allow west >= east (dateline crossing)
        if (clampedWest >= clampedEast) {
          console.warn(
            `MapView: Dateline-crossing bounds detected: west=${clampedWest} >= east=${clampedEast} (this is valid for dateline crossing)`
          );
        }

        // For datasets requiring minimum zoom (like MODIS 1km), zoom to center at required level
        if (minZoom) {
          const centerLat = (clampedSouth + clampedNorth) / 2;
          const centerLon = (clampedWest + clampedEast) / 2;

          console.log(
            `MapView: Using minimum zoom ${minZoom} centered at [${centerLat.toFixed(4)}, ${centerLon.toFixed(4)}]`
          );
          map.setView([centerLat, centerLon], minZoom);
        } else {
          // Normal behavior: fit bounds for best view
          const bounds = [
            [south, west], // southwest [lat, lng]
            [north, east], // northeast [lat, lng]
          ];
          map.fitBounds(bounds, { padding: [20, 20] });
        }

        console.log(
          '? Updated map view to bbox:',
          bbox,
          'using provider:',
          mapProvider,
          minZoom ? `(minZoom: ${minZoom})` : ''
        );
      } catch (error) {
        console.error('? Error updating map view:', error);
      }
    }
  };

  // Process comparison user query - NEW UNIFIED FLOW
  // Uses ComparisonAgent on backend to parse query and execute dual STAC searches
  // Triggered either by: (1) user typing in chat after selecting comparison module (awaitingUserQuery=true)
  // or (2) Get Started button dispatching a comparison query directly (awaitingUserQuery may be false)
  useEffect(() => {
    if (!comparisonUserQuery) {
      return;
    }

    console.log('MapView: Processing comparison user query:', comparisonUserQuery);

    // Ensure comparison mode is enabled (may not be if triggered from Get Started button)
    setComparisonMode(true);

    // Function to process the comparison query using the unified comparison agent
    const processComparisonQuery = async () => {
      try {
        // Show thinking message
        if (onGeointAnalysis) {
          onGeointAnalysis({
            type: 'thinking',
            message: 'Analyzing your comparison request...',
          });
        }

        // Use pin coordinates if available, otherwise fall back to map center
        let fallbackLat: number | undefined;
        let fallbackLng: number | undefined;
        if (pinState.active && pinState.lat != null && pinState.lng != null) {
          fallbackLat = pinState.lat;
          fallbackLng = pinState.lng;
          console.log(
            `MapView: Using pin coords for comparison: (${fallbackLat.toFixed(4)}, ${fallbackLng.toFixed(4)})`
          );
        } else if (map) {
          const center = map.getCenter();
          if (center) {
            fallbackLat = center.lat;
            fallbackLng = center.lng;
          }
        }

        // Call the unified comparison endpoint with user_query
        // The backend ComparisonAgent will:
        // 1. Parse location, dates, and analysis type from the query
        // 2. Execute dual STAC searches (before/after)
        // 3. Return tile URLs for both time periods
        console.log('MapView: Calling unified /api/geoint/comparison endpoint...');
        const comparisonRequestBody: any = {
          user_query: comparisonUserQuery,
          latitude: fallbackLat,
          longitude: fallbackLng,
        };
        const response = await authenticatedFetch(`${API_BASE_URL}/api/geoint/comparison`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(comparisonRequestBody),
        });

        if (!response.ok) {
          let errorMessage = `Comparison analysis failed: ${response.statusText}`;
          try {
            const errorData = await response.json();
            if (errorData.message) {
              errorMessage = errorData.message;
            } else if (errorData.detail) {
              errorMessage = errorData.detail;
            }
          } catch {
            // If JSON parsing fails, use status text
          }
          throw new Error(errorMessage);
        }

        const data = await response.json();
        console.log('MapView: Comparison agent response:', data);

        // Reset awaiting flag
        setComparisonState((prev) => ({ ...prev, awaitingUserQuery: false }));

        // Handle prompt response (initial click without query)
        if (data.type === 'prompt') {
          if (onGeointAnalysis) {
            onGeointAnalysis({
              type: 'assistant',
              message:
                data.message ||
                'Please specify the location, date range, and what you would like to compare.',
            });
          }
          return;
        }

        // Handle error response
        if (data.status === 'error') {
          throw new Error(data.message || 'Comparison analysis failed');
        }

        // Handle successful comparison response
        const result = data.result || data;

        if (result.type === 'comparison' && result.before && result.after) {
          console.log('MapView: Processing comparison result with before/after data');

          // Enable comparison mode
          setComparisonMode(true);

          // Store the before/after imagery data
          setComparisonState((prev) => ({
            ...prev,
            beforeImagery: result.before,
            afterImagery: result.after,
            showingBefore: true, // Start by showing BEFORE view
          }));

          // Fly to the location if bbox is provided
          if (result.bbox && map) {
            const [west, south, east, north] = result.bbox;
            map.fitBounds(
              [
                [south, west],
                [north, east],
              ],
              { padding: [20, 20] }
            );
          }

          // Render BEFORE tiles on the map
          if (result.before.tile_urls && result.before.tile_urls.length > 0) {
            console.log('MapView: Rendering BEFORE tiles:', result.before.tile_urls);

            // Get the TileJSON to extract the actual tile template
            try {
              const tileJsonUrl = result.before.tile_urls[0];
              const tileJsonResponse = await fetch(tileJsonUrl);
              if (tileJsonResponse.ok) {
                const tileJson = await tileJsonResponse.json();
                console.log('MapView: BEFORE TileJSON:', tileJson);

                // Set satellite data to trigger tile layer rendering
                setSatelliteData({
                  bbox: result.bbox || tileJson.bounds,
                  items: result.before.stac_items || [],
                  tile_url: tileJson.tiles?.[0] || tileJsonUrl,
                  preview_url: undefined,
                });
              }
            } catch (tileError) {
              console.warn('MapView: Error fetching BEFORE TileJSON:', tileError);
            }
          }

          // Display the analysis summary
          if (onGeointAnalysis) {
            const beforeDisplay = result.before.datetime_display || result.before.datetime;
            const afterDisplay = result.after.datetime_display || result.after.datetime;

            onGeointAnalysis({
              type: 'assistant',
              message:
                result.analysis ||
                `**Comparison Mode Active**\n\n` +
                  `**Location:** ${result.location}\n` +
                  `**Before:** ${beforeDisplay} (${result.before.features_count || 0} scenes)\n` +
                  `**After:** ${afterDisplay} (${result.after.features_count || 0} scenes)\n\n` +
                  `Use the **BEFORE/AFTER** toggle buttons on the map to switch between time periods.`,
            });
          }

          // ── VISION ANALYSIS: Capture before/after screenshots of rendered STAC tiles ──
          // After tiles are rendered on the map, capture screenshots of each period
          // and send them to GPT-5 Vision for AI-powered change detection analysis
          const hasBothTiles =
            result.before.tile_urls?.length > 0 && result.after.tile_urls?.length > 0;
          if (hasBothTiles) {
            // Fire-and-forget: capture + analyze in background so the user sees tiles immediately
            (async () => {
              try {
                console.log(
                  '[SNAP] Comparison: Starting dual screenshot capture for Vision analysis...'
                );

                // Helper: strip data-URL prefix
                const stripPrefix = (s: string) => {
                  if (s.startsWith('data:image/png;base64,'))
                    return s.replace('data:image/png;base64,', '');
                  if (s.startsWith('data:image/jpeg;base64,'))
                    return s.replace('data:image/jpeg;base64,', '');
                  return s;
                };

                // Helper: render a tile URL set on the map and wait for tiles to load
                const renderAndCapture = async (
                  tileUrls: string[],
                  label: string
                ): Promise<string | null> => {
                  const tileJsonUrl = tileUrls[0];
                  const tjResp = await fetch(tileJsonUrl);
                  if (!tjResp.ok) return null;
                  const tj = await tjResp.json();

                  setSatelliteData({
                    bbox: result.bbox || tj.bounds,
                    items: [],
                    tile_url: tj.tiles?.[0] || tileJsonUrl,
                    preview_url: undefined,
                  });

                  // Give the map time to render the new tile layer
                  await new Promise((r) => setTimeout(r, 2000));

                  const snap = await captureMapScreenshot();
                  if (snap && snap.length > 1000) {
                    console.log(
                      `[SNAP] Comparison: ${label} screenshot captured (${Math.round(snap.length / 1024)}KB)`
                    );
                    return stripPrefix(snap);
                  }
                  console.warn(`[SNAP] Comparison: ${label} screenshot too small or failed`);
                  return null;
                };

                // 1) BEFORE tiles are already rendered — capture directly
                // Wait for tiles to finish loading (they were set above via setSatelliteData)
                await new Promise((r) => setTimeout(r, 2000));

                const beforeSnap = await captureMapScreenshot();
                const beforeScreenshot =
                  beforeSnap && beforeSnap.length > 1000 ? stripPrefix(beforeSnap) : null;
                if (beforeScreenshot) {
                  console.log(
                    `[SNAP] Comparison: BEFORE screenshot captured (${Math.round(beforeScreenshot.length / 1024)}KB)`
                  );
                } else {
                  console.warn('[SNAP] Comparison: BEFORE screenshot failed');
                }

                // 2) Render AFTER tiles and capture
                const afterScreenshot = await renderAndCapture(result.after.tile_urls, 'AFTER');

                // 3) Switch back to BEFORE view (user expects to start on BEFORE)
                if (result.before.tile_urls?.length > 0) {
                  try {
                    const tjResp = await fetch(result.before.tile_urls[0]);
                    if (tjResp.ok) {
                      const tj = await tjResp.json();
                      setSatelliteData({
                        bbox: result.bbox || tj.bounds,
                        items: result.before.stac_items || [],
                        tile_url: tj.tiles?.[0] || result.before.tile_urls[0],
                        preview_url: undefined,
                      });
                    }
                  } catch {
                    /* best effort */
                  }
                }

                if (!beforeScreenshot && !afterScreenshot) {
                  console.warn(
                    '[SNAP] Comparison: Both screenshots failed — skipping Vision analysis'
                  );
                  return;
                }

                // 4) Send both screenshots to backend for GPT-5 Vision change detection
                console.log(
                  '[SNAP] Comparison: Sending before/after screenshots for Vision analysis...'
                );
                if (onGeointAnalysis) {
                  onGeointAnalysis({
                    type: 'thinking',
                    message: 'Analyzing visual differences between before and after imagery...',
                  });
                }

                const center_lat = (result.bbox[1] + result.bbox[3]) / 2;
                const center_lng = (result.bbox[0] + result.bbox[2]) / 2;

                const visionResp = await authenticatedFetch(
                  `${API_BASE_URL}/api/geoint/comparison`,
                  {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                      latitude: center_lat,
                      longitude: center_lng,
                      before_date: result.before.datetime || result.before.best_scene_date,
                      after_date: result.after.datetime || result.after.best_scene_date,
                      before_screenshot: beforeScreenshot,
                      after_screenshot: afterScreenshot,
                      user_query: comparisonUserQuery,
                      comparison_aspect: result.analysis_type || 'general',
                      collection_id: result.collection,
                      download_rasters: false, // We already have screenshots, skip raster download
                    }),
                  }
                );

                if (visionResp.ok) {
                  const visionData = await visionResp.json();
                  const visionText = visionData.result?.text || visionData.result?.analysis;
                  if (visionText && onGeointAnalysis) {
                    onGeointAnalysis({
                      type: 'assistant',
                      message: `**AI Change Detection Analysis**\n\n${visionText}`,
                    });
                    console.log('[SNAP] Comparison: Vision analysis complete');
                  }
                } else {
                  console.warn(
                    `[SNAP] Comparison: Vision analysis request failed: ${visionResp.status}`
                  );
                }
              } catch (visionErr) {
                console.error('[SNAP] Comparison: Vision analysis error:', visionErr);
              }
            })();
          }
        } else {
          // Response has analysis text but no before/after tile data
          // This can happen if the tool didn't execute or returned no imagery
          console.warn('MapView: Comparison response missing before/after data:', {
            type: result.type,
            hasBefore: !!result.before,
            hasAfter: !!result.after,
            hasAnalysis: !!result.analysis,
            toolCalls: result.tool_calls,
          });
          if (onGeointAnalysis) {
            const analysisText =
              result.analysis ||
              'Comparison analysis completed but no imagery tiles were returned.';
            onGeointAnalysis({
              type: 'assistant',
              message:
                analysisText +
                (result.before || result.after
                  ? ''
                  : '\n\n*No before/after imagery tiles were found. Try a different collection, date range, or location.*'),
            });
          }
        }
      } catch (error) {
        console.error('MapView: Error processing comparison query:', error);
        if (onGeointAnalysis) {
          onGeointAnalysis({
            type: 'error',
            message: `Failed to process comparison request: ${error instanceof Error ? error.message : 'Unknown error'}`,
          });
        }
        setComparisonState((prev) => ({ ...prev, awaitingUserQuery: false }));
      }
    };

    processComparisonQuery();
  }, [comparisonUserQuery]);

  // Update map based on selected dataset
  useEffect(() => {
    if (!map || !selectedDataset) return;

    // You can add dataset-specific map updates here
    // For example, adding data layers, changing view, etc.
    console.log('Selected dataset changed:', selectedDataset);
  }, [map, selectedDataset]);

  // Dynamic tile expansion when user zooms out
  // NOTE: This is DISABLED for mosaic-based tiles as they already handle zoom/pan seamlessly.
  // Mosaic tiles (without item= parameter) work across all zoom levels.
  // Individual item tiles (with item= parameter) only serve tiles within their geographic footprint.
  useEffect(() => {
    if (!map || !mapLoaded || !satelliteData || !originalBounds || !lastCollection) return;

    // FIX: Skip expansion for mosaic-based tiles
    // Mosaic tiles already handle zoom/pan seamlessly across all zoom levels.
    // Only individual item tiles need expansion when user zooms out.
    const isMosaicTileUrl = (url: string | undefined): boolean => {
      if (!url) return false;
      // Mosaic URLs don't have item= parameter - they use collection-level mosaics
      // which automatically serve tiles across the entire collection's extent
      return !url.includes('item=') && url.includes('planetarycomputer.microsoft.com');
    };

    if (isMosaicTileUrl(satelliteData.tile_url)) {
      console.log(
        'ℹ️ MapView: Skipping zoom expansion - mosaic tiles already handle zoom/pan seamlessly'
      );
      return;
    }

    let expansionTimeoutId: NodeJS.Timeout | null = null;

    const handleZoomChange = async () => {
      try {
        const currentCamera = map.getCamera();
        const currentBounds = currentCamera.bounds;

        if (!currentBounds || isExpanding) return;

        // Enhanced bounds validation to prevent null coordinate errors
        const isValidBounds = (bounds: any) => {
          return (
            bounds &&
            Array.isArray(bounds) &&
            bounds.length === 4 &&
            bounds.every(
              (coord: any) =>
                typeof coord === 'number' &&
                !isNaN(coord) &&
                isFinite(coord) &&
                coord >= -180 &&
                coord <= 180
            )
          );
        };

        if (!isValidBounds(currentBounds) || !isValidBounds(originalBounds)) {
          console.warn('??? MapView: Invalid bounds detected, skipping zoom expansion:', {
            currentBounds,
            originalBounds,
          });
          return;
        }

        // Calculate expansion ratio - how much larger is the current view vs original
        const originalWidth = Math.abs(originalBounds[2] - originalBounds[0]);
        const originalHeight = Math.abs(originalBounds[3] - originalBounds[1]);
        const currentWidth = Math.abs(currentBounds[2] - currentBounds[0]);
        const currentHeight = Math.abs(currentBounds[3] - currentBounds[1]);

        const widthRatio = currentWidth / originalWidth;
        const heightRatio = currentHeight / originalHeight;
        const expansionRatio = Math.max(widthRatio, heightRatio);

        // If user has zoomed out significantly (3x or more), fetch expanded data
        if (expansionRatio >= 3.0) {
          console.log('?? MapView: User zoomed out significantly, requesting expanded tiles');
          console.log('?? MapView: Expansion ratio:', expansionRatio.toFixed(2));
          console.log('?? MapView: Original bounds:', originalBounds);
          console.log('?? MapView: Current bounds:', currentBounds);

          setIsExpanding(true);

          // Calculate expanded bounding box with padding and validation
          const paddingWidth = currentWidth * 0.1;
          const paddingHeight = currentHeight * 0.1;

          const expandedBbox = [
            Math.max(-180, Math.min(currentBounds[0], originalBounds[0]) - paddingWidth),
            Math.max(-85, Math.min(currentBounds[1], originalBounds[1]) - paddingHeight),
            Math.min(180, Math.max(currentBounds[2], originalBounds[2]) + paddingWidth),
            Math.min(85, Math.max(currentBounds[3], originalBounds[3]) + paddingHeight),
          ];

          // Final validation of expanded bbox
          if (!isValidBounds(expandedBbox)) {
            console.error(
              '??? MapView: Generated invalid expanded bbox, aborting expansion:',
              expandedBbox
            );
            setIsExpanding(false);
            return;
          }

          console.log('MapView: Requesting expanded bbox:', expandedBbox);

          // Clear any existing timeout
          if (expansionTimeoutId) {
            clearTimeout(expansionTimeoutId);
          }

          // Set a safety timeout to clear the indicator after 10 seconds max
          expansionTimeoutId = setTimeout(() => {
            console.log('MapView: Expansion timeout reached, clearing indicator');
            setIsExpanding(false);
            expansionTimeoutId = null;
          }, 10000);

          try {
            // FIX: Use direct STAC search endpoint with explicit bbox
            // Previously used /api/query with generic text which caused wrong location results
            const apiUrl = `${API_BASE_URL}/api/stac-search`;
            console.log('MapView: Expansion API URL:', apiUrl);
            console.log('MapView: Using direct STAC search with bbox:', expandedBbox);
            console.log('MapView: Collection:', lastCollection);

            const response = await authenticatedFetch(apiUrl, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                collections: [lastCollection],
                bbox: expandedBbox,
                limit: 50,
                sortby: [{ field: 'datetime', direction: 'desc' }],
              }),
            });

            if (response.ok) {
              const data = await response.json();
              console.log('MapView: Expansion STAC response:', data);

              // FIX: Handle direct STAC search response format
              // Format: { success: true, results: { features: [...] } }
              const stacFeatures = data.results?.features || [];

              if (stacFeatures.length > 0) {
                console.log(`MapView: Expansion found ${stacFeatures.length} STAC items`);

                // Calculate union bbox from all features
                const tileBboxes = stacFeatures
                  .map((f: { bbox?: number[] }) => f.bbox)
                  .filter((b: number[] | undefined) => b && b.length === 4);

                if (tileBboxes.length > 0) {
                  const unionBbox = [
                    Math.min(...tileBboxes.map((b: number[]) => b[0])),
                    Math.min(...tileBboxes.map((b: number[]) => b[1])),
                    Math.max(...tileBboxes.map((b: number[]) => b[2])),
                    Math.max(...tileBboxes.map((b: number[]) => b[3])),
                  ];

                  if (isValidBounds(unionBbox)) {
                    console.log('MapView: Applying expanded tile coverage from STAC search');
                    console.log('MapView: Union bbox:', unionBbox);
                    console.log('MapView: Feature count:', stacFeatures.length);

                    // Build tile URLs from STAC features using backend-cleaned tilejson URLs
                    // The backend's clean_tilejson_urls() applies proper rendering params for each collection
                    // (e.g., SST uses sea_surface_temperature asset + turbo colormap, not generic visual asset)
                    const allTileUrls = stacFeatures.map(
                      (feature: {
                        id: string;
                        bbox?: number[];
                        collection?: string;
                        assets?: { tilejson?: { href?: string } };
                      }) => {
                        // Extract tilejson URL from feature assets (cleaned by backend with proper rendering params)
                        const backendTilejsonUrl = feature.assets?.tilejson?.href;
                        // Fallback to generic URL only if backend didn't provide one
                        const fallbackUrl = `https://planetarycomputer.microsoft.com/api/data/v1/item/tilejson.json?collection=${feature.collection || lastCollection}&item=${feature.id}&assets=visual&asset_bidx=visual%7C1%2C2%2C3`;

                        const tilejsonUrl = backendTilejsonUrl || fallbackUrl;
                        if (backendTilejsonUrl) {
                          console.log(
                            `MapView: Using backend-cleaned tilejson URL for ${feature.id}`
                          );
                        } else {
                          console.warn(
                            `MapView: No tilejson in assets for ${feature.id}, using fallback`
                          );
                        }

                        return {
                          item_id: feature.id,
                          bbox: feature.bbox,
                          tilejson_url: tilejsonUrl,
                        };
                      }
                    );

                    // Build satellite data structure matching initial response format
                    const expandedSatelliteData = {
                      bbox: unionBbox,
                      tile_url: allTileUrls[0]?.tilejson_url,
                      all_tile_urls: allTileUrls,
                      items: stacFeatures.map(
                        (f: {
                          id: string;
                          collection?: string;
                          properties?: { datetime?: string };
                          bbox?: number[];
                        }) => ({
                          id: f.id,
                          collection: f.collection || lastCollection,
                          datetime: f.properties?.datetime || new Date().toISOString(),
                          bbox: f.bbox,
                        })
                      ),
                    };

                    setSatelliteData(expandedSatelliteData);
                    setOriginalBounds(unionBbox);
                    console.log(
                      'MapView: Successfully expanded tile coverage via direct STAC search'
                    );
                  } else {
                    console.error('MapView: Expansion returned invalid union bbox:', unionBbox);
                  }
                } else {
                  console.warn('MapView: No valid bboxes in expansion features');
                }
              } else {
                // Fallback: Check for legacy response formats
                const allTileUrls = data.translation_metadata?.all_tile_urls;

                if (allTileUrls && Array.isArray(allTileUrls) && allTileUrls.length > 0) {
                  console.log(
                    `MapView: Expansion returned ${allTileUrls.length} tile URLs (legacy format)`
                  );

                  // Calculate union bbox from all tile URLs
                  const tileBboxes: number[][] = allTileUrls
                    .map((t: { bbox?: number[] }) => t.bbox)
                    .filter((b): b is number[] => b !== undefined && b.length === 4);
                  if (tileBboxes.length > 0) {
                    const unionBbox = [
                      Math.min(...tileBboxes.map((b) => b[0])),
                      Math.min(...tileBboxes.map((b) => b[1])),
                      Math.max(...tileBboxes.map((b) => b[2])),
                      Math.max(...tileBboxes.map((b) => b[3])),
                    ];

                    if (isValidBounds(unionBbox)) {
                      console.log('MapView: Applying expanded tile coverage');
                      console.log('MapView: Union bbox:', unionBbox);
                      console.log('MapView: Tile count:', allTileUrls.length);

                      // Build satellite data structure matching initial response format
                      const expandedSatelliteData = {
                        bbox: unionBbox,
                        tile_url: allTileUrls[0]?.tilejson_url,
                        all_tile_urls: allTileUrls,
                        items: [],
                      };

                      setSatelliteData(expandedSatelliteData);
                      setOriginalBounds(unionBbox);
                      console.log('MapView: Successfully expanded tile coverage');
                    } else {
                      console.error('MapView: Expansion returned invalid union bbox:', unionBbox);
                    }
                  } else {
                    console.warn('MapView: No valid bboxes in expansion tile URLs');
                  }
                } else {
                  console.log(
                    'ℹ️ MapView: No tile data in expansion response, keeping current tiles'
                  );
                }
              }
            } else {
              console.error(
                '? MapView: Expansion API request failed:',
                response.status,
                response.statusText
              );
              const errorText = await response.text().catch(() => 'Unknown error');
              console.error('? MapView: Expansion error details:', errorText);
            }
          } catch (error) {
            console.error('? MapView: Network error during tile expansion:', error);
          }

          // Clear the expansion timeout since operation completed
          if (expansionTimeoutId) {
            clearTimeout(expansionTimeoutId);
            expansionTimeoutId = null;
          }

          setIsExpanding(false);
        }
      } catch (error) {
        console.error('? MapView: Error during tile expansion:', error);

        // Clear timeout on error
        if (expansionTimeoutId) {
          clearTimeout(expansionTimeoutId);
          expansionTimeoutId = null;
        }

        setIsExpanding(false);
      }
    };

    // Add zoom change listener
    if (map.on) {
      map.on('zoomend', handleZoomChange);
      map.on('moveend', handleZoomChange);

      return () => {
        if (expansionTimeoutId) {
          clearTimeout(expansionTimeoutId);
        }
        map.off('zoomend', handleZoomChange);
        map.off('moveend', handleZoomChange);
      };
    }
  }, [map, mapLoaded, mapProvider, satelliteData, originalBounds, lastCollection, isExpanding]);

  // Track zoom level changes and update state for UI
  useEffect(() => {
    if (!map || !mapLoaded) return;

    const updateZoomLevel = () => {
      setCurrentZoomLevel(Math.round(map.getZoom()));
    };

    updateZoomLevel();

    if (map.on) {
      map.on('zoomend', updateZoomLevel);
      return () => map.off('zoomend', updateZoomLevel);
    }
  }, [map, mapLoaded, mapProvider]);

  // Reset isExpanding when new satellite data is loaded from a user query (not zoom expansion)
  // This ensures the "Adjusting tiles" indicator is hidden when:
  // 1. User submits a new query
  // 2. Map is fully covered with tiles
  // 3. User zooms back in
  useEffect(() => {
    if (satelliteData && isExpanding) {
      // Wait a moment to ensure expansion is complete, then hide indicator
      const timeout = setTimeout(() => {
        setIsExpanding(false);
        console.log('MapView: Cleared expansion indicator');
      }, 500);
      return () => clearTimeout(timeout);
    }
  }, [satelliteData]);

  // Terrain analysis click handler
  const handleTerrainAnalysisClick = async (lat: number, lng: number) => {
    console.log(`MapView: Terrain analysis pin placed at (${lat.toFixed(6)}, ${lng.toFixed(6)})`);

    // Cancel any pending thinking messages from previous terrain analysis
    // This handles the case where user repositions pin while analysis is in progress
    if (onGeointAnalysis) {
      onGeointAnalysis({ type: 'cancel_thinking' });
    }

    // Set terrain session with coordinates immediately (sessionId will be populated after API returns)
    // This allows Chat.tsx to detect terrain mode is active and wait for the session ID
    if (onTerrainSessionChange) {
      onTerrainSessionChange({ sessionId: null, lat, lng });
    }

    // Remove existing terrain pin if present
    if (terrainAnalysisPin.marker && window.L) {
      map.removeLayer(terrainAnalysisPin.marker);
    }

    // Create modern pin marker
    let newMarker: any = null;
    try {
      if (window.L) {
        const pinIcon = window.L.divIcon({
          html: `
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#3B82F6" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="filter: drop-shadow(0 2px 4px rgba(0,0,0,0.3));">
              <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" fill="#3B82F6"></path>
              <circle cx="12" cy="10" r="3" fill="white"></circle>
            </svg>
          `,
          className: 'terrain-pin-marker',
          iconSize: [32, 32],
          iconAnchor: [16, 32],
        });

        newMarker = window.L.marker([lat, lng], {
          icon: pinIcon,
          draggable: false,
        }).addTo(map);
      }

      // Update terrain analysis pin state
      setTerrainAnalysisPin({
        lat,
        lng,
        marker: newMarker,
      });

      // Capture screenshot of current map view
      console.log('[SNAP] MapView: Capturing screenshot for terrain analysis...');

      // Check zoom level - satellite tiles require zoom >= 6
      // If too zoomed out, zoom in to see satellite imagery
      const MIN_ZOOM_FOR_TILES = 10; // Good zoom for terrain analysis
      let currentZoom = 0;

      currentZoom = map.getZoom();

      console.log(
        `[SNAP] MapView: Current zoom: ${currentZoom}, min for tiles: 6, recommended: ${MIN_ZOOM_FOR_TILES}`
      );

      if (currentZoom < MIN_ZOOM_FOR_TILES) {
        console.log(
          `[SNAP] MapView: Zooming in from ${currentZoom} to ${MIN_ZOOM_FOR_TILES} for better satellite imagery`
        );
        map.setView([lat, lng], MIN_ZOOM_FOR_TILES, { animate: false });

        // Wait for tiles to load at new zoom level
        console.log('[SNAP] MapView: Waiting for tiles to load at new zoom...');
        await new Promise((resolve) => setTimeout(resolve, 2500));
      }

      // Wait for Leaflet tiles to finish loading before capturing
      await new Promise((resolve) => setTimeout(resolve, 800));

      const screenshot = await captureMapScreenshot();

      if (!screenshot) {
        console.error('MapView: Failed to capture screenshot');
        if (onGeointAnalysis) {
          onGeointAnalysis({
            type: 'error',
            message:
              'Failed to capture the map screenshot. The map tiles may still be loading. Please wait a moment for tiles to finish loading and try again.',
          });
        }
        return;
      }

      // Detect screenshot format (JPEG vs PNG)
      const isJPEG = screenshot.startsWith('data:image/jpeg');
      const isPNG = screenshot.startsWith('data:image/png');
      const format = isJPEG ? 'JPEG' : isPNG ? 'PNG' : 'UNKNOWN';

      console.log(`[SNAP] MapView: Screenshot format: ${format}`);

      // Strip the data URL prefix to get just the base64 string
      // captureMapScreenshot returns "data:image/jpeg;base64,xxxxx" or "data:image/png;base64,xxxxx"
      // Backend expects just "xxxxx"
      let base64Screenshot = screenshot;
      if (screenshot.startsWith('data:image/jpeg;base64,')) {
        base64Screenshot = screenshot.replace('data:image/jpeg;base64,', '');
      } else if (screenshot.startsWith('data:image/png;base64,')) {
        base64Screenshot = screenshot.replace('data:image/png;base64,', '');
      }

      console.log(
        `MapView: Screenshot captured (${format}, ${base64Screenshot.length} chars, ~${Math.round(base64Screenshot.length / 1024)}KB)`
      );

      // Validate screenshot isn't too small (likely empty canvas)
      if (base64Screenshot.length < 1000) {
        console.error('MapView: Screenshot is too small, likely empty canvas');
        if (onGeointAnalysis) {
          onGeointAnalysis({
            type: 'error',
            message:
              'Map screenshot appears to be empty. The map tiles may not have loaded yet. Please wait a moment and try again.',
          });
        }
        return;
      }

      // Get current map bounds and zoom level for context
      let bounds: any = null;
      let zoomLevel: number = 0;

      const leafletBounds = map.getBounds();
      bounds = {
        north: leafletBounds.getNorth(),
        south: leafletBounds.getSouth(),
        east: leafletBounds.getEast(),
        west: leafletBounds.getWest(),
      };
      zoomLevel = map.getZoom();

      console.log('MapView: Sending terrain analysis request to backend...');
      console.log(`[PIN] Pin location: (${lat}, ${lng})`);
      console.log(`Zoom level: ${zoomLevel}`);
      console.log(`[PKG] Bounds:`, bounds);

      // Disable terrain analysis mode after pin is placed
      setTerrainAnalysisMode(false);

      // Store the screenshot in vision state so Chat can access it via mapContext
      setVisionScreenshot(base64Screenshot);

      // Prompt user to type their terrain question instead of auto-analyzing
      if (onGeointAnalysis) {
        onGeointAnalysis({
          type: 'terrain_ready',
          message: '**Pin Placed**\n\nWhat would you like to know about this location?',
          coordinates: { lat, lng },
        });
      }
    } catch (error) {
      console.error('MapView: Error in terrain analysis:', error);
      if (onGeointAnalysis) {
        onGeointAnalysis({
          type: 'error',
          message: `Failed to analyze terrain: ${error}`,
        });
      }
    }
  };

  // Attach map click listener for pin placement
  useEffect(() => {
    if (!map || !mapLoaded) return;

    const handleMapClick = (e: any) => {
      const lat: number = e.latlng.lat;
      const lng: number = e.latlng.lng;

      // Check if we're in terrain analysis mode
      if (terrainAnalysisMode) {
        handleTerrainAnalysisClick(lat, lng);
        return;
      }

      // Allow pin mode if either pinMode is true OR a module is selected
      if (!pinMode && !selectedModule) return;

      handleMapClickForPin(lat, lng);
    };

    // Add click listener
    if (map.on) {
      map.on('click', handleMapClick);

      return () => {
        map.off('click', handleMapClick);
      };
    }
  }, [map, mapLoaded, mapProvider, pinMode, selectedModule, pinState.marker, terrainAnalysisMode]);

  // Notify parent when pin changes
  const prevPinStateRef = useRef<{ active: boolean; lat: number | null; lng: number | null }>({
    active: false,
    lat: null,
    lng: null,
  });

  useEffect(() => {
    // Only notify if the pin state actually changed
    const hasChanged =
      prevPinStateRef.current.active !== pinState.active ||
      prevPinStateRef.current.lat !== pinState.lat ||
      prevPinStateRef.current.lng !== pinState.lng;

    if (!hasChanged) {
      return; // No change, skip notification
    }

    // Update the ref to track current state
    prevPinStateRef.current = {
      active: pinState.active,
      lat: pinState.lat,
      lng: pinState.lng,
    };

    if (onPinChange) {
      if (pinState.active && pinState.lat !== null && pinState.lng !== null) {
        onPinChange({ lat: pinState.lat, lng: pinState.lng });
        console.log(
          `?? MapView: Notifying parent of pin change: (${pinState.lat.toFixed(4)}, ${pinState.lng.toFixed(4)})`
        );
      } else {
        onPinChange(null);
        console.log('?? MapView: Notifying parent that pin was cleared');
      }
    }
  }, [pinState.active, pinState.lat, pinState.lng]); // Removed onPinChange from dependencies

  // Track if we're currently rendering to prevent re-entry during async operations
  const isRenderingRef = useRef(false);

  // FIX: Track the last successfully rendered satellite data to prevent duplicate renders
  const lastRenderedDataRef = useRef<string | null>(null);

  // This ref lets the styledata handler re-add the layers that were wiped.
  const activeTileLayersRef = useRef<any[]>([]);

  // Create a stable signature for satellite data to detect true changes
  const getSatelliteDataSignature = (data: SatelliteData | null): string | null => {
    if (!data) return null;
    // Use tile_url + item count + first item ID as a unique signature
    const firstItemId = data.items?.[0]?.id || 'none';
    const itemCount = data.items?.length || 0;
    return `${data.tile_url || 'no-url'}_${itemCount}_${firstItemId}`;
  };

  // Reset rendering flag when TRULY new satellite data arrives (different signature)
  useEffect(() => {
    const newSignature = getSatelliteDataSignature(satelliteData);
    if (newSignature !== lastRenderedDataRef.current) {
      // This is genuinely new data - allow rendering
      isRenderingRef.current = false;
    }
    // If signature matches, don't reset - we already rendered this
  }, [satelliteData]);

  // Add satellite imagery to map when data is available
  useEffect(() => {
    // FIX: Check if we've already rendered this exact data
    const currentSignature = getSatelliteDataSignature(satelliteData);
    if (currentSignature && currentSignature === lastRenderedDataRef.current) {
      console.log('MapView: Skipping re-render - already rendered this satellite data');
      return;
    }

    // PREVENT RE-ENTRY: If already rendering, skip to avoid clearing tiles mid-render
    if (isRenderingRef.current) {
      return; // Skip silently
    }

    // More specific validation - check for essential data
    if (!satelliteData) {
      return; // Skip silently
    }

    if (!map) {
      return; // Skip silently
    }

    if (!satelliteData.tile_url) {
      console.log(
        '??? MapView: No tile URL available - this collection may contain non-visualizable data (like GOES-GLM)'
      );
      console.log('??? MapView: Available satellite data items:', satelliteData.items?.length || 0);

      // Still zoom to the geographic area if we have location data
      if (
        satelliteData.bbox &&
        Array.isArray(satelliteData.bbox) &&
        satelliteData.bbox.length === 4 &&
        map
      ) {
        console.log('??? MapView: Zooming to collection area despite no visualizable data');
        const [west, south, east, north] = satelliteData.bbox;

        try {
          // Validate coordinates
          if (!isNaN(west) && !isNaN(south) && !isNaN(east) && !isNaN(north)) {
            const bboxArea = Math.abs(east - west) * Math.abs(north - south);
            let targetZoom = bboxArea < 0.1 ? 12 : bboxArea < 2 ? 8 : 6;

            console.log(
              '??? MapView: Setting camera for non-visualizable data with zoom:',
              targetZoom
            );
            map.setCamera({
              bounds: [west, south, east, north],
              zoom: targetZoom,
              padding: 50,
            });
          }
        } catch (error) {
          console.error('??? MapView: Error setting camera for non-visualizable data:', error);
        }
      }
      return;
    }

    if (!mapLoaded) {
      console.log('??? MapView: Skipping satellite data rendering - map not loaded yet');
      return;
    }

    console.log('??? MapView: ? Requirements met - proceeding with satellite data rendering');
    console.log('??? MapView: Adding satellite data to map:', satelliteData);
    console.log('??? MapView: Current mapProvider:', mapProvider);
    console.log('??? MapView: Map instance type:', map?.constructor?.name || 'unknown');

    // Initialize original bounds and collection for dynamic expansion
    if (satelliteData.bbox && !originalBounds) {
      setOriginalBounds(satelliteData.bbox);
      console.log('??? MapView: Set original bounds for dynamic expansion:', satelliteData.bbox);
    }

    // Track collection type for expansion queries
    if (satelliteData.items && satelliteData.items.length > 0) {
      const collection = satelliteData.items[0].collection;
      if (collection !== lastCollection) {
        setLastCollection(collection);
        console.log('??? MapView: Tracking collection for expansion:', collection);

        // TEMPORARILY DISABLED: Legends are showing incorrect data
        // TODO: Re-enable once legend data is fixed
        // const hasVisualizableData = !!satelliteData.tile_url;
        // setShowDataLegend(hasVisualizableData);
        setShowDataLegend(false);

        // if (hasVisualizableData) {
        //   console.log('[ART] MapView: Showing data legend for collection:', collection);
        // }
      }
    }

    try {
      // Remove existing satellite layer if any
      if (currentLayer) {
        console.log('??? MapView: Removing existing Leaflet layer');
        map.removeLayer(currentLayer);
        setCurrentLayer(null);
      }

      // If we have a tile URL, add it as a tile layer
      if (satelliteData.tile_url && window.L) {
        console.log('??? MapView: Adding Leaflet tile layer:', satelliteData.tile_url);

        // Configure tile layer options with bounds to prevent 404s outside data coverage
        const tileLayerOptions: any = {
          opacity: 0.8,
          attribution: 'Planetary Computer',
          errorTileUrl:
            'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', // Transparent 1x1 pixel
          maxNativeZoom: 18, // Prevent requests beyond available zoom levels
          tileSize: 256,
        };

        // Add bounds if available to limit tile requests to actual data coverage
        // This prevents excessive 404 errors for tiles outside the STAC item's bbox
        if (satelliteData.bbox && satelliteData.bbox.length === 4) {
          const [west, south, east, north] = satelliteData.bbox;
          tileLayerOptions.bounds = window.L.latLngBounds(
            window.L.latLng(south, west),
            window.L.latLng(north, east)
          );
          console.log('??? MapView: Tile layer constrained to bbox:', satelliteData.bbox);
        }

        const addTileLayer = (tileUrl: string) => {
          const tileLayer = window.L.tileLayer(tileUrl, tileLayerOptions);

          // Suppress tile load errors in console (404s outside bounds are expected)
          tileLayer.on('tileerror', (error: any) => {
            // Silently handle tile errors - they're expected for tiles outside data coverage
            // Only log if it's a repeated pattern that might indicate a real issue
            const url = error.tile?.src || 'unknown';
            if (Math.random() < 0.01) {
              // Log only 1% of errors to avoid console spam
              console.debug(
                '? MapView: Tile not available (expected for tiles outside data bounds):',
                url.substring(0, 120)
              );
            }
          });

          tileLayer.addTo(map);
          setCurrentLayer(tileLayer);
          console.log('? MapView: Successfully added Leaflet satellite tile layer');
        };

        if (satelliteData.is_mosaic) {
          // tile_url is a TileJSON URL (JSON endpoint) — must fetch it to get the XYZ tile template
          console.log(
            '??? MapView: Mosaic detected — fetching TileJSON to resolve XYZ tile template...'
          );
          const collection = satelliteData.items?.[0]?.collection;
          fetchAndSignTileJSON(satelliteData.tile_url, { collection }).then((result) => {
            if (result.success && result.tileTemplate) {
              console.log(
                '??? MapView: Resolved mosaic tile template:',
                result.tileTemplate.substring(0, 120)
              );
              addTileLayer(result.tileTemplate);
            } else {
              console.error(
                '??? MapView: Failed to resolve mosaic TileJSON, falling back to raw URL:',
                result.error
              );
              if (satelliteData.tile_url) {
                addTileLayer(satelliteData.tile_url);
              }
            }
          });
        } else if (satelliteData.tile_url) {
          addTileLayer(satelliteData.tile_url);
        }
      }

      // If we have map_data GeoJSON, add it as vector data
      if (lastChatResponse?.map_data?.features && window.L) {
        console.log('??? MapView: Adding GeoJSON features to Leaflet');

        lastChatResponse.map_data.features.forEach((feature: any) => {
          const geoJsonLayer = window.L.geoJSON(feature, {
            style: {
              fillColor: 'rgba(0, 0, 255, 0.2)',
              fillOpacity: 0.3,
              color: 'blue',
              weight: 2,
            },
          });

          geoJsonLayer.addTo(map);
        });

        console.log('? MapView: Successfully added GeoJSON featu to Leaflet');
      }

      // CRITICAL FIX: DO NOT call updateMapView here!
      // The map view update is ALREADY handled in the useEffect that processes lastChatResponse (line ~1186)
      // Calling updateMapView here with stale satelliteData.bbox causes the map to briefly pan to old coordinates
      // when a new query arrives (e.g., asking for Greece but map flashes to Australia from previous query)
      // See: https://github.com/facebook/react/issues/14920 - stale closure in useEffect

      // OLD CODE (caused race condition):
      // if (satelliteData.bbox) {
      //   console.log('??? MapView: Updating map view to bbox:', satelliteData.bbox);
      //   updateMapView(satelliteData.bbox);
      // }

      console.log(
        '??? MapView: Satellite data layer added - map view update handled by lastChatResponse processor'
      );
    } catch (error) {
      console.error('? MapView: Error adding satellite data to map:', error);
    }
  }, [satelliteData, map, mapLoaded]); // REMOVED lastChatResponse from dependencies to prevent stale bbox race condition

  // Pin button click handler - always opens module selection menu
  const handlePinButtonClick = () => {
    console.log('[PIN] MapView: Pin button clicked');

    // ALWAYS open modules menu when pin button is clicked
    // User should always see the module selection, regardless of current state
    console.log('[PIN] MapView: Opening modules menu');
    const newMenuState = !showModulesMenu;
    setShowModulesMenu(newMenuState);

    if (newMenuState && onModulesMenuOpen) {
      // Notify parent that modules menu opened (MainApp will display the message)
      onModulesMenuOpen();
    }
  };

  // Module selection handler - TOGGLES off if clicking the same module
  const handleModuleSelect = (module: string) => {
    console.log('MapView: Module clicked:', module, 'Current:', selectedModule);

    // TOGGLE OFF: If clicking the already-selected module, deselect it
    if (selectedModule === module) {
      console.log('[SYNC] MapView: Deselecting module (toggle off):', module);
      setSelectedModule(null);
      setPinMode(false);
      setTerrainAnalysisMode(false);
      setComparisonMode(false);
      setShowModulesMenu(false);
      setVisionMode(false);
      setVisionScreenshot(null); // Clear vision screenshot when deselecting

      // Clear any existing pin marker from map
      if (pinState.marker && map) {
        try {
          map.markers.remove(pinState.marker);
        } catch (e) {
          console.log('Could not remove pin marker');
        }
      }
      // Reset pin state
      setPinState({ lat: null, lng: null, active: false, marker: null });
      // Notify parent that pin was cleared
      if (onPinChange) {
        onPinChange(null);
      }

      // Notify parent that module was deselected
      if (onModuleSelected) {
        onModuleSelected(null);
      }

      // Send deselection message to chat
      if (onGeointAnalysis) {
        onGeointAnalysis({
          type: 'module_deselected',
          message:
            '**Analysis Mode Deactivated**\n\nYou can now use regular chat queries. Click a module to re-enable analysis mode.',
        });
      }

      return;
    }

    // SELECT NEW MODULE
    setSelectedModule(module);

    // Handle comparison module - pin-first workflow like other GEOINT modules
    if (module === 'comparison') {
      console.log('MapView: Comparison module selected - enabling pin mode for location selection');
      setComparisonMode(true);
      setPinMode(true);

      if (onModuleSelected) {
        onModuleSelected(module);
      }

      if (onGeointAnalysis) {
        onGeointAnalysis({
          type: 'module_selected',
          message:
            '**Comparison Analysis**\n\n**Step 1:** Navigate to the area you want to compare, then **drop a pin** on the location.\n**Step 2:** In the chat, describe what to compare (e.g., *"Compare vegetation between January 2020 and January 2024"*).\n\nThe system will load before and after satellite imagery and analyze the changes.',
        });
      }

      setShowModulesMenu(false);
      return;
    }

    // For other modules, enable pin mode automatically
    setPinMode(true);
    console.log('[PIN] MapView: Pin mode automatically enabled for module:', module);

    // Notify parent that module was selected
    if (onModuleSelected) {
      onModuleSelected(module);
    }

    // Show appropriate chat message based on module
    let message = '';

    if (module === 'vision') {
      message =
        '**Vision Analysis Activated**\n\nDrop a pin on the location you want to analyze. All your questions will use AI vision to analyze the visible imagery.';
      console.log('MapView: Vision module selected');
    } else if (module === 'terrain') {
      message = '**Terrain Analysis Selected**\n\nDrop a pin on the location you want to analyze.';
      setTerrainAnalysisMode(true);
    } else if (module === 'mobility') {
      message =
        '**Mobility Assessment Selected**\n\nDrop **Pin A** (start point) on the map, then click again to drop **Pin B** (destination).';
      // Reset any previous mobility pins
      if (mobilityPinA?.marker) {
        if (window.L) map?.removeLayer(mobilityPinA.marker);
      }
      if (mobilityPinB?.marker) {
        if (window.L) map?.removeLayer(mobilityPinB.marker);
      }
      setMobilityPinA(null);
      setMobilityPinB(null);
      mobilityPinARef.current = null;
      mobilityPinBRef.current = null;
    } else if (module === 'building_damage') {
      message =
        '**Building Damage Assessment Selected**\n\nDrop a pin on the building you want to analyze.';
    } else if (module === 'extreme_weather') {
      message =
        '**Extreme Weather Analysis Selected**\n\nDrop a pin on the location you want to analyze.';
    } else if (module === 'timeseries') {
      message =
        '**Time Series Animation Selected**\n\nDrop a pin on the location you want to analyze.';
    }

    // Send message to chat
    if (onGeointAnalysis && message) {
      onGeointAnalysis({
        type: 'module_selected',
        message: message,
      });
    }

    // Close modules menu after selection
    setShowModulesMenu(false);
  };

  // Handle Before/After toggle for comparison mode
  const toggleBeforeAfter = async () => {
    console.log('MapView: Toggling between BEFORE and AFTER views');

    const newShowingBefore = !comparisonState.showingBefore;
    setComparisonState((prev) => ({ ...prev, showingBefore: newShowingBefore }));

    // Switch the rendered imagery - supports both old and new data formats
    const imageryToRender = newShowingBefore
      ? comparisonState.beforeImagery
      : comparisonState.afterImagery;

    if (!imageryToRender) {
      console.warn('MapView: No imagery data available for toggle');
      return;
    }

    console.log(
      `MapView: Rendering ${newShowingBefore ? 'BEFORE' : 'AFTER'} imagery`,
      imageryToRender
    );

    // NEW FORMAT: ComparisonAgent returns { tile_urls: [...], stac_items: [...], datetime: "..." }
    if (imageryToRender.tile_urls && imageryToRender.tile_urls.length > 0) {
      try {
        const tileJsonUrl = imageryToRender.tile_urls[0];
        console.log(`MapView: Fetching TileJSON from: ${tileJsonUrl}`);

        const tileJsonResponse = await fetch(tileJsonUrl);
        if (tileJsonResponse.ok) {
          const tileJson = await tileJsonResponse.json();
          console.log('MapView: TileJSON loaded:', tileJson);

          setSatelliteData({
            bbox: tileJson.bounds || satelliteData?.bbox,
            items: imageryToRender.stac_items || [],
            tile_url: tileJson.tiles?.[0] || tileJsonUrl,
            preview_url: undefined,
          });

          console.log(`MapView: ${newShowingBefore ? 'BEFORE' : 'AFTER'} imagery rendered`);
        } else {
          console.warn(`MapView: Failed to fetch TileJSON: ${tileJsonResponse.status}`);
        }
      } catch (error) {
        console.error('MapView: Error loading tile data:', error);
      }
    }
    // OLD FORMAT: Legacy data with data.stac_results
    else if (imageryToRender.data && imageryToRender.data.stac_results) {
      const stacData = imageryToRender.data.stac_results;

      const satelliteDataFormat = {
        bbox: stacData.bbox || satelliteData?.bbox,
        items: stacData.features || [],
        tile_url:
          stacData.features?.[0]?.assets?.tilejson?.href ||
          stacData.features?.[0]?.assets?.rendered_preview?.href,
        preview_url: stacData.features?.[0]?.assets?.thumbnail?.href,
      };

      setSatelliteData(satelliteDataFormat);
      console.log(
        `MapView: ${newShowingBefore ? 'BEFORE' : 'AFTER'} imagery rendered (legacy format)`
      );
    }

    // Notify user via chat
    if (onGeointAnalysis) {
      const dateDisplay = imageryToRender.datetime_display || imageryToRender.datetime || '';
      onGeointAnalysis({
        type: 'info',
        message: `Switched to ${newShowingBefore ? 'BEFORE' : 'AFTER'} view${dateDisplay ? ` (${dateDisplay})` : ''}`,
      });
    }
  };

  // Map click handler for pin placement
  const handleMapClickForPin = async (lat: number, lng: number) => {
    if (!pinMode || !map || !selectedModule) {
      console.log(
        '[PIN] MapView: Pin drop cancelled - pinMode:',
        pinMode,
        'selectedModule:',
        selectedModule
      );
      return;
    }

    // Comparison module: Pin drop captures location + screenshot, then awaits temporal query in chat
    if (selectedModule === 'comparison') {
      console.log(`[PIN] MapView: Comparison pin placed at (${lat.toFixed(4)}, ${lng.toFixed(4)})`);

      // Place a pin marker on the map
      let marker: any = null;
      if (mapProvider === 'leaflet' && window.L) {
        const pinIcon = window.L.divIcon({
          html: `
            <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="filter: drop-shadow(0 2px 4px rgba(0,0,0,0.3));">
              <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" fill="#f59e0b"></path>
              <circle cx="12" cy="10" r="3" fill="white" stroke="none"></circle>
            </svg>
          `,
          className: 'comparison-pin',
          iconSize: [36, 36],
          iconAnchor: [18, 36],
        });
        marker = window.L.marker([lat, lng], { icon: pinIcon, draggable: false }).addTo(map);
        marker
          .bindPopup(`Comparison Pin<br/>Lat: ${lat.toFixed(4)}°<br/>Lng: ${lng.toFixed(4)}°`)
          .openPopup();
      }

      setPinState({ lat, lng, active: true, marker });
      if (onPinChange) {
        onPinChange({ lat, lng });
      }

      // Wait for map to render with the pin, then capture screenshot
      await new Promise((resolve) => setTimeout(resolve, 500));

      try {
        const captured = await captureMapScreenshot();
        if (captured) {
          let clean = captured;
          if (clean.startsWith('data:image/png;base64,'))
            clean = clean.replace('data:image/png;base64,', '');
          else if (clean.startsWith('data:image/jpeg;base64,'))
            clean = clean.replace('data:image/jpeg;base64,', '');
          if (clean.length > 1000) {
            setVisionScreenshot(clean);
            console.log(
              `[SNAP] MapView: Comparison pin screenshot captured (${Math.round(clean.length / 1024)}KB)`
            );
          }
        }
      } catch (snapErr) {
        console.warn('[SNAP] MapView: Failed to capture comparison pin screenshot:', snapErr);
      }

      // Set state to await user's temporal query
      setComparisonState((prev) => ({ ...prev, awaitingUserQuery: true }));

      // Disable pin mode — pin is placed, now waiting for chat input
      setPinMode(false);

      // Prompt user to enter comparison query
      if (onGeointAnalysis) {
        onGeointAnalysis({
          type: 'assistant',
          message: '**Pin Placed**\n\nWhat would you like to know about this location?',
        });
      }

      return;
    }

    // Terrain module should use handleTerrainAnalysisClick instead
    // This handles the case where user clicks a second time after first terrain pin
    if (selectedModule === 'terrain') {
      console.log('[PIN] MapView: Redirecting terrain module click to handleTerrainAnalysisClick');
      handleTerrainAnalysisClick(lat, lng);
      return;
    }

    // Mobility module: Two-pin A->B workflow
    if (selectedModule === 'mobility') {
      console.log(
        `[PIN] MapView: Mobility two-pin workflow at (${lat.toFixed(4)}, ${lng.toFixed(4)})`
      );

      if (!mobilityPinARef.current) {
        // First click: Place Pin A (green - start point)
        let markerA: any = null;
        if (window.L) {
          const pinIconA = window.L.divIcon({
            html: `
              <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="#16a34a" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="filter: drop-shadow(0 2px 4px rgba(0,0,0,0.3));">
                <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" fill="#16a34a"></path>
                <text x="12" y="14" text-anchor="middle" fill="white" font-size="10" font-weight="bold" stroke="none">A</text>
              </svg>
            `,
            className: 'mobility-pin-a',
            iconSize: [36, 36],
            iconAnchor: [18, 36],
          });
          markerA = window.L.marker([lat, lng], { icon: pinIconA, draggable: false }).addTo(map);
          markerA
            .bindPopup(`Pin A (Start)<br/>Lat: ${lat.toFixed(4)}°<br/>Lng: ${lng.toFixed(4)}°`)
            .openPopup();
        }

        mobilityPinARef.current = { lat, lng, marker: markerA };
        setMobilityPinA({ lat, lng, marker: markerA });

        if (onGeointAnalysis) {
          onGeointAnalysis({
            type: 'info',
            message: `**Pin A set** at (${lat.toFixed(4)}°, ${lng.toFixed(4)}°)\n\nNow click on the map to drop **Pin B** (destination).`,
          });
        }
        return;
      }

      // Second click: Place Pin B (red - destination) and trigger analysis
      // Remove existing Pin B if repositioning
      if (mobilityPinBRef.current?.marker) {
        if (window.L) map.removeLayer(mobilityPinBRef.current.marker);
      }

      let markerB: any = null;
      if (window.L) {
        const pinIconB = window.L.divIcon({
          html: `
            <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="#dc2626" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="filter: drop-shadow(0 2px 4px rgba(0,0,0,0.3));">
              <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" fill="#dc2626"></path>
              <text x="12" y="14" text-anchor="middle" fill="white" font-size="10" font-weight="bold" stroke="none">B</text>
            </svg>
          `,
          className: 'mobility-pin-b',
          iconSize: [36, 36],
          iconAnchor: [18, 36],
        });
        markerB = window.L.marker([lat, lng], { icon: pinIconB, draggable: false }).addTo(map);
        markerB
          .bindPopup(`Pin B (Destination)<br/>Lat: ${lat.toFixed(4)}°<br/>Lng: ${lng.toFixed(4)}°`)
          .openPopup();
      }

      mobilityPinBRef.current = { lat, lng, marker: markerB };
      setMobilityPinB({ lat, lng, marker: markerB });

      // Capture a screenshot of the current map view for mobility vision analysis
      // This gives the agent visual context (e.g., nearby airports, roads, landmarks)
      try {
        const screenshot = await captureMapScreenshot();
        if (screenshot) {
          let base64 = screenshot;
          if (screenshot.startsWith('data:image/jpeg;base64,')) {
            base64 = screenshot.replace('data:image/jpeg;base64,', '');
          } else if (screenshot.startsWith('data:image/png;base64,')) {
            base64 = screenshot.replace('data:image/png;base64,', '');
          }
          if (base64.length > 1000) {
            setVisionScreenshot(base64);
            console.log(
              `[SNAP] MapView: Mobility screenshot captured (${Math.round(base64.length / 1024)}KB)`
            );
          }
        }
      } catch (e) {
        console.warn('[SNAP] MapView: Failed to capture mobility screenshot:', e);
      }

      // Both pins placed — prompt user to type their mobility question
      const pinA = mobilityPinARef.current!;
      if (onGeointAnalysis) {
        onGeointAnalysis({
          type: 'mobility_ready',
          message: '**Pins Placed**\n\nWhat would you like to know about this route?',
          coordinates: { lat: pinA.lat, lng: pinA.lng },
          coordinatesB: { lat, lng },
        });
      }
      return;
    }

    console.log(
      `[PIN] MapView: Placing pin at (${lat.toFixed(4)}, ${lng.toFixed(4)}) for module: ${selectedModule}`
    );

    // Remove existing marker if present
    if (pinState.marker) {
      if (window.L) {
        map.removeLayer(pinState.marker);
      }
    }

    // Create new marker
    let newMarker: any = null;
    try {
      if (window.L) {
        // Modern SVG blue pin for Leaflet (matches terrain analysis style)
        const pinIcon = window.L.divIcon({
          html: `
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#3B82F6" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="filter: drop-shadow(0 2px 4px rgba(0,0,0,0.3));">
              <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" fill="#3B82F6"></path>
              <circle cx="12" cy="10" r="3" fill="white"></circle>
            </svg>
          `,
          className: 'geoint-pin-marker',
          iconSize: [32, 32],
          iconAnchor: [16, 32],
        });

        newMarker = window.L.marker([lat, lng], {
          icon: pinIcon,
          draggable: false,
        }).addTo(map);

        // Add popup with coordinates
        newMarker
          .bindPopup(`Pin Location<br/>Lat: ${lat.toFixed(4)}°<br/>Lng: ${lng.toFixed(4)}°`)
          .openPopup();
      }

      setPinState({
        lat,
        lng,
        active: true,
        marker: newMarker,
      });

      console.log('MapView: Pin placed successfully (can be repositioned while in pin mode)');

      // Cancel any ongoing analysis when pin is repositioned
      if (analysisInProgress && analysisAbortControllerRef.current) {
        console.log('[STOP] MapView: Cancelling previous analysis - pin repositioned');
        analysisAbortControllerRef.current.abort();
        analysisAbortControllerRef.current = null;
      }

      // Send notification to chat about pin drop and trigger analysis
      if (onGeointAnalysis) {
        console.log(`[PIN] MapView: Triggering ${selectedModule} analysis at`, lat, lng);

        setAnalysisInProgress(true);

        // Create new AbortController for this analysis
        const abortController = new AbortController();
        analysisAbortControllerRef.current = abortController;

        // Map UI module names to API module names and get display names
        let apiModule = '';
        let query = '';
        let displayName = '';
        switch (selectedModule) {
          case 'vision':
            apiModule = 'vision';
            query = 'Analyze the imagery at this location';
            displayName = 'Vision';
            break;
          case 'terrain':
            apiModule = 'terrain'; // Backend expects 'terrain' not 'terrain_analysis'
            query = 'Describe the terrain features in this location';
            displayName = 'Terrain';
            break;
          case 'mobility':
            apiModule = 'mobility'; // Backend expects 'mobility' not 'mobility_analysis'
            query = 'Analyze mobility and trafficability in this location';
            displayName = 'Mobility';
            break;
          case 'building_damage':
            apiModule = 'building_damage';
            query = 'Assess building damage in this location';
            displayName = 'Building Damage';
            break;
          case 'extreme_weather':
            apiModule = 'extreme_weather';
            query = 'Analyze extreme weather and climate projections at this location';
            displayName = 'Extreme Weather';
            break;
          case 'comparison':
            // Comparison module should NEVER reach here - it uses text queries, not pins
            console.error('MapView: Comparison module incorrectly triggered with pin drop');
            onGeointAnalysis({
              type: 'error',
              message:
                '**Comparison Module Error**\n\nThe comparison module does not use pin drops. Please use the chat to describe what you want to compare.',
            });
            setAnalysisInProgress(false);
            analysisAbortControllerRef.current = null;
            return;
          default:
            console.error(`MapView: Unknown module selected: ${selectedModule}`);
            onGeointAnalysis({
              type: 'error',
              message: `**Unknown Module**\n\nThe selected module "${selectedModule}" is not recognized. Please select a valid analysis module.`,
            });
            setAnalysisInProgress(false);
            analysisAbortControllerRef.current = null;
            return;
        }

        // For Building Damage, Vision, and Extreme Weather: capture screenshot but wait for user question in Chat
        let screenshot: string | undefined = undefined;
        if (selectedModule === 'building_damage') {
          // BUILDING DAMAGE MODULE: Enforce zoom level and wait for user question
          // Require zoom >= 16 so the user is zoomed in to individual buildings
          if (currentZoomLevel < 16) {
            console.log(
              `MapView: Building damage zoom check failed (zoom=${currentZoomLevel}, need >= 16)`
            );
            onGeointAnalysis({
              type: 'error',
              message: `**Zoom In Required**\n\nBuilding damage assessment requires a close-up view of individual buildings. Please zoom in closer (current zoom: ${currentZoomLevel}, required: 16+) and drop a pin directly on the building you want to assess.`,
            });
            setAnalysisInProgress(false);
            analysisAbortControllerRef.current = null;
            return;
          }

          console.log(
            'MapView: Building Damage module - capturing screenshot for user question...'
          );

          // Wait for map to render (give it time to show the pin)
          await new Promise((resolve) => setTimeout(resolve, 500));

          // Capture screenshot for context (will be sent via mapContext)
          const capturedScreenshot = await captureMapScreenshot();

          if (capturedScreenshot) {
            let cleanScreenshot = capturedScreenshot;
            if (cleanScreenshot.startsWith('data:image/png;base64,')) {
              cleanScreenshot = cleanScreenshot.replace('data:image/png;base64,', '');
            } else if (cleanScreenshot.startsWith('data:image/jpeg;base64,')) {
              cleanScreenshot = cleanScreenshot.replace('data:image/jpeg;base64,', '');
            }
            console.log(
              `MapView: Building damage screenshot captured (${Math.round(cleanScreenshot.length / 1024)}KB)`
            );
            setVisionScreenshot(cleanScreenshot);
          } else {
            console.warn('MapView: Failed to capture building damage screenshot');
            setVisionScreenshot(null);
          }

          // Update vision pin state (will be included in mapContext)
          setVisionPin({ lat, lng });
          setVisionMode(true);

          // Show message prompting user to ask a question
          onGeointAnalysis({
            type: 'building_damage_ready',
            message: '**Pin Placed**\n\nWhat would you like to know about this building?',
            coordinates: { lat, lng },
          });

          // Reset analysis flag - we're not auto-analyzing
          setAnalysisInProgress(false);
          analysisAbortControllerRef.current = null;

          // Exit early - don't trigger automatic analysis
          return;
        } else if (selectedModule === 'vision') {
          // VISION MODULE: Capture context but DON'T auto-analyze
          // User will type a question in Chat which will trigger the analysis
          console.log('MapView: Vision module - capturing context for user question...');

          // Wait for map to render (give it time to show the pin)
          await new Promise((resolve) => setTimeout(resolve, 500));

          // Capture screenshot for context (will be sent via mapContext)
          const capturedScreenshot = await captureMapScreenshot();

          if (capturedScreenshot) {
            // Strip data URL prefix for backend - handle both jpeg and png
            let cleanScreenshot = capturedScreenshot;
            if (cleanScreenshot.startsWith('data:image/png;base64,')) {
              cleanScreenshot = cleanScreenshot.replace('data:image/png;base64,', '');
            } else if (cleanScreenshot.startsWith('data:image/jpeg;base64,')) {
              cleanScreenshot = cleanScreenshot.replace('data:image/jpeg;base64,', '');
            }
            console.log(
              `MapView: Vision context screenshot captured (${Math.round(cleanScreenshot.length / 1024)}KB)`
            );
            // Store the screenshot for use in mapContext
            setVisionScreenshot(cleanScreenshot);
          } else {
            console.warn('MapView: Failed to capture vision screenshot');
            setVisionScreenshot(null);
          }

          // Update vision pin state (will be included in mapContext)
          setVisionPin({ lat, lng });
          setVisionMode(true);

          // Show message prompting user to ask a question
          onGeointAnalysis({
            type: 'vision_ready',
            message: '**Pin Placed**\n\nWhat would you like to know about this location?',
            coordinates: { lat, lng },
          });

          // Reset analysis flag - we're not auto-analyzing
          setAnalysisInProgress(false);
          analysisAbortControllerRef.current = null;

          // Exit early - don't trigger automatic analysis
          return;
        } else if (selectedModule === 'extreme_weather') {
          // EXTREME WEATHER MODULE: Capture screenshot + lat/lng but DON'T auto-analyze
          // User will type a climate question in Chat which will trigger the analysis
          console.log(
            'MapView: Extreme Weather module - capturing location and screenshot for user question...'
          );

          // Wait for map to render (give it time to show the pin)
          await new Promise((resolve) => setTimeout(resolve, 500));

          // Capture screenshot for visual context
          const capturedScreenshot = await captureMapScreenshot();
          if (capturedScreenshot) {
            let cleanScreenshot = capturedScreenshot;
            if (cleanScreenshot.startsWith('data:image/png;base64,')) {
              cleanScreenshot = cleanScreenshot.replace('data:image/png;base64,', '');
            } else if (cleanScreenshot.startsWith('data:image/jpeg;base64,')) {
              cleanScreenshot = cleanScreenshot.replace('data:image/jpeg;base64,', '');
            }
            console.log(
              `MapView: Extreme weather screenshot captured (${Math.round(cleanScreenshot.length / 1024)}KB)`
            );
            setVisionScreenshot(cleanScreenshot);
          } else {
            console.warn('MapView: Failed to capture extreme weather screenshot');
            setVisionScreenshot(null);
          }

          // Update vision pin state (will be included in mapContext)
          setVisionPin({ lat, lng });
          setVisionMode(true);

          // Show message prompting user to ask a climate question
          onGeointAnalysis({
            type: 'extreme_weather_ready',
            message: '**Pin Placed**\n\nWhat would you like to know about this location?',
            coordinates: { lat, lng },
          });

          // Reset analysis flag - we're not auto-analyzing
          setAnalysisInProgress(false);
          analysisAbortControllerRef.current = null;

          // Exit early - don't trigger automatic analysis
          return;
        } else {
          // OTHER MODULES (terrain, etc.) - Auto-analyze with screenshot
          // Capture screenshot for visual context before triggering analysis
          await new Promise((resolve) => setTimeout(resolve, 500));

          if (!screenshot) {
            const capturedScreenshot = await captureMapScreenshot();
            if (capturedScreenshot) {
              screenshot = capturedScreenshot.startsWith('data:image/png;base64,')
                ? capturedScreenshot.replace('data:image/png;base64,', '')
                : capturedScreenshot.startsWith('data:image/jpeg;base64,')
                  ? capturedScreenshot.replace('data:image/jpeg;base64,', '')
                  : capturedScreenshot;
              console.log(
                `MapView: Screenshot captured for ${selectedModule} (${Math.round(screenshot.length / 1024)}KB)`
              );
            }
          }

          // Show "Analyzing..." message for other modules
          onGeointAnalysis({
            type: 'pending',
            message: `**Analyzing ${displayName.toLowerCase()}...**\n\nProcessing satellite imagery at coordinates (${lat.toFixed(4)}°, ${lng.toFixed(4)}°).`,
            coordinates: { lat, lng },
          });
        }

        // Trigger analysis based on selected module
        try {
          const { triggerGeointAnalysis } = await import('../services/api');

          const result = await triggerGeointAnalysis(
            lat,
            lng,
            apiModule,
            query,
            `Selected module: ${selectedModule}`,
            screenshot, // Pass screenshot (only populated for building_damage)
            abortController.signal // Pass abort signal
          );

          // Send results to chat
          onGeointAnalysis({
            type: 'complete',
            data: result,
            module: selectedModule,
            coordinates: { lat, lng },
          });

          // Reset analysis flag to allow new analysis
          setAnalysisInProgress(false);
          analysisAbortControllerRef.current = null;
        } catch (error) {
          // Check if this was an abort (user repositioned pin)
          if (error instanceof Error && error.name === 'AbortError') {
            console.log('[SKIP] MapView: Analysis cancelled (pin repositioned)');
            // Don't show error to user - this is intentional cancellation
            return;
          }

          console.error('MapView: GEOINT analysis failed:', error);
          onGeointAnalysis({
            type: 'error',
            message: `**Analysis Failed**\n\n${error instanceof Error ? error.message : 'Unknown error occurred'}`,
            coordinates: { lat, lng },
          });

          // Reset analysis flag even on error
          setAnalysisInProgress(false);
          analysisAbortControllerRef.current = null;
        }
      } else if (analysisInProgress) {
        console.log('[WAIT] MapView: Analysis already in progress, pin moved but not re-analyzing');
      }
    } catch (error) {
      console.error('MapView: Error placing pin marker:', error);
    }
  };

  // Clear pin handler
  const handleClearPin = () => {
    console.log('?? MapView: Clearing pin');

    // Cancel any ongoing analysis
    if (analysisAbortControllerRef.current) {
      console.log('[STOP] MapView: Cancelling ongoing analysis - pin cleared');
      analysisAbortControllerRef.current.abort();
      analysisAbortControllerRef.current = null;
    }

    // Remove marker from map
    if (pinState.marker && map) {
      try {
        if (window.L) {
          map.removeLayer(pinState.marker);
        }
      } catch (error) {
        console.error('? MapView: Error removing pin marker:', error);
      }
    }

    // Reset pin state
    setPinState({
      lat: null,
      lng: null,
      active: false,
      marker: null,
    });

    // Reset analysis flag
    setAnalysisInProgress(false);

    console.log('[DEL] MapView: Pin cleared');
  };

  // Zoom in handler
  const handleZoomIn = () => {
    if (!map) return;
    try {
      map.zoomIn();
      console.log('[+] MapView: Zoomed in');
    } catch (error) {
      console.error('MapView: Error zooming in:', error);
    }
  };

  // Zoom out handler
  const handleZoomOut = () => {
    if (!map) return;
    try {
      map.zoomOut();
      console.log('[-] MapView: Zoomed out');
    } catch (error) {
      console.error('MapView: Error zooming out:', error);
    }
  };

  // Reset bearing/rotation handler (no-op: Leaflet does not support bearing/pitch)
  const handleResetBearing = () => {};

  // Enhanced dataset visualization using collection config
  const getDatasetVisualization = (dataset: Dataset | null) => {
    if (!dataset)
      return { emoji: '???', description: 'Interactive satellite map', color: '#f8f9fa' };

    // Use the new collection configuration system
    try {
      const visualization = getCollectionVisualization(dataset.id);
      return {
        emoji: visualization.emoji,
        description:
          getCollectionConfig(dataset.id)?.description ||
          dataset.description ||
          `${dataset.title} visualization`,
        color: visualization.color,
      };
    } catch (error) {
      console.warn('??? MapView: Failed to get collection visualization, using fallback:', error);
      return {
        emoji: '??',
        description: `${dataset.title} visualization`,
        color: '#f8f9fa',
      };
    }
  };

  const visualization = getDatasetVisualization(selectedDataset);

  // ================================================================
  // Track camera position changes (moveend) to update map context
  // This ensures that after navigate_to, the map bounds are refreshed
  // so follow-up queries like "what is on the map" get correct coords.
  // ================================================================
  useEffect(() => {
    if (!map || !mapLoaded) return;

    const handlePositionChange = () => {
      setMapPositionVersion((v) => v + 1);
    };

    if (map.on) {
      map.on('moveend', handlePositionChange);
      map.on('zoomend', handlePositionChange);
      return () => {
        map.off('moveend', handlePositionChange);
        map.off('zoomend', handlePositionChange);
      };
    }
  }, [map, mapLoaded, mapProvider]);

  // Update map context for Chat Vision capability
  // This provides the chat with current map state for vision-based queries
  useEffect(() => {
    if (!onMapContextChange) return;

    // Only need map to be loaded - don't require satelliteData
    // This allows vision queries to work even without explicitly loaded STAC imagery
    if (!map || !mapLoaded) {
      onMapContextChange(null);
      return;
    }

    try {
      // Get current map bounds
      let bounds = null;
      if (window.L) {
        const leafletBounds = map.getBounds();
        bounds = {
          north: leafletBounds.getNorth(),
          south: leafletBounds.getSouth(),
          east: leafletBounds.getEast(),
          west: leafletBounds.getWest(),
          center_lat: map.getCenter().lat,
          center_lng: map.getCenter().lng,
        };
      }

      // DON'T capture screenshot here - it should only happen when user triggers vision query
      // Screenshot capture was causing errors (canvas not ready) and is unnecessary for context updates
      // The screenshot will be captured on-demand when user clicks vision mode, drops a pin, and sends a query

      // Get the current collection for all tiles
      const currentCollection = satelliteData?.items?.[0]?.collection || lastCollection || null;

      // Build tile URLs array from satelliteData for Vision Agent
      const tileUrls =
        satelliteData?.all_tile_urls?.map((tile: { tilejson_url: string; item_id: string }) => ({
          tilejson_url: tile.tilejson_url,
          item_id: tile.item_id,
          collection: currentCollection,
        })) || [];

      // Build STAC items array with assets for Vision Agent raster analysis (NDVI, etc.)
      const stacItems =
        satelliteData?.items?.map(
          (item: {
            id: string;
            collection: string;
            bbox?: number[];
            datetime: string;
            assets?: Record<string, unknown>;
          }) => ({
            id: item.id,
            collection: item.collection,
            bbox: item.bbox,
            properties: {
              datetime: item.datetime,
            },
            assets: item.assets || {}, // Include band URLs (B04, B08, etc.) for NDVI computation
          })
        ) || [];

      // Build map context - include vision screenshot if available
      const context = {
        bounds: bounds,
        imagery_base64:
          (visionMode ||
            selectedModule === 'terrain' ||
            selectedModule === 'mobility' ||
            selectedModule === 'extreme_weather' ||
            selectedModule === 'comparison' ||
            selectedModule === 'building_damage') &&
          visionScreenshot
            ? visionScreenshot
            : null, // Include screenshot for all GEOINT modules
        current_collection: currentCollection,
        tile_urls: tileUrls, // TiTiler URLs for Vision Agent raster analysis
        stac_items: stacItems, // Full STAC items with assets for NDVI computation
        item_id: satelliteData?.items?.[0]?.id || null,
        datetime: satelliteData?.items?.[0]?.datetime || null,
        // Azure Maps was removed, so the `atlas` types are gone; this branch only
        // runs for a non-Leaflet provider, of which there is currently none.
        zoom_level:
          mapProvider === 'leaflet' ? map.getZoom() : (map as any).getCamera().zoom,
        has_satellite_data: !!satelliteData, // Flag to indicate if STAC imagery is loaded
        vision_mode: visionMode, // explicit vision mode flag
        vision_pin: visionMode ? visionPin : null, // pin coordinates for vision analysis
      };

      console.log('MapView: Updated map context for Chat Vision:', {
        has_screenshot: !!(visionMode && visionScreenshot),
        screenshot_size: visionScreenshot
          ? `${Math.round(visionScreenshot.length / 1024)}KB`
          : 'none',
        has_satellite_data: context.has_satellite_data,
        vision_mode: visionMode,
        vision_pin: visionMode ? visionPin : null,
        collection: context.current_collection,
        tile_urls_count: tileUrls.length,
        stac_items_count: stacItems.length,
        bounds: context.bounds,
      });

      onMapContextChange(context);
    } catch (error) {
      console.error('MapView: Error updating map context:', error);
      onMapContextChange(null);
    }
  }, [
    satelliteData,
    map,
    mapLoaded,
    mapProvider,
    onMapContextChange,
    lastCollection,
    visionMode,
    visionPin,
    visionScreenshot,
    mapPositionVersion,
    selectedModule,
  ]);

  return (
    <div className="map" style={{ position: 'relative' }}>
      {/* Always render map container so mapRef.current is available */}
      <div
        ref={mapRef}
        style={{
          width: '100%',
          height: '100%',
          display: 'block',
        }}
      />

      {/* Loading overlay - only show when map is not loaded */}
      {!mapLoaded && (
        <div
          style={{
            position: 'absolute',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            backgroundColor: 'rgba(255, 255, 255, 0.9)',
            zIndex: 1000,
            color: 'var(--muted)',
            fontSize: '16px',
            padding: '20px',
          }}
        >
          <div style={{ marginBottom: '10px' }}>{t('map.loading')}</div>
        </div>
      )}

      {/* Map status overlay - only show when there's an active dataset */}
      {mapLoaded && selectedDataset && (
        <div
          style={{
            position: 'absolute',
            top: '10px',
            left: '10px',
            background: 'rgba(255, 255, 255, 0.95)',
            padding: '8px 12px',
            borderRadius: '6px',
            fontSize: '14px',
            fontWeight: '500',
            boxShadow: '0 2px 8px rgba(0, 0, 0, 0.15)',
            zIndex: 1000,
            border: '1px solid rgba(0, 0, 0, 0.1)',
          }}
        >
          <div>
            <div style={{ fontSize: '12px', color: '#666', marginBottom: '2px' }}>
              {t('map.viewingDataset')}
            </div>
            <div style={{ color: '#333' }}>{selectedDataset.title}</div>
          </div>
        </div>
      )}

      {/* Pin controls - show when map is loaded */}
      {mapLoaded && (
        <>
          {/* Pin toggle button - Modern icon-only button */}
          <div
            onClick={handlePinButtonClick}
            title={t('map.geointModules')}
            style={{
              position: 'absolute',
              top: '10px',
              left: '10px', // Moved to top left
              background: showModulesMenu ? 'rgba(34, 197, 94, 0.85)' : 'rgba(255, 255, 255, 0.3)',
              color: showModulesMenu ? 'white' : '#333',
              width: '48px',
              height: '48px',
              borderRadius: '12px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '24px',
              boxShadow: '0 2px 12px rgba(0, 0, 0, 0.15)',
              zIndex: 1000,
              border: showModulesMenu
                ? '2px solid rgba(34, 197, 94, 1)'
                : '1px solid rgba(0, 0, 0, 0.15)',
              cursor: 'pointer',
              userSelect: 'none',
              transition: 'all 0.2s ease',
              backdropFilter: 'blur(10px)',
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.transform = 'scale(1.08)';
              e.currentTarget.style.boxShadow = '0 4px 16px rgba(0, 0, 0, 0.2)';
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.transform = 'scale(1)';
              e.currentTarget.style.boxShadow = '0 2px 12px rgba(0, 0, 0, 0.15)';
            }}
          >
            {/* Modern Pin Icon - SVG */}
            <svg
              width="24"
              height="24"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              style={{ display: 'block' }}
            >
              <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"></path>
              <circle cx="12" cy="10" r="3"></circle>
            </svg>
          </div>

          {/* Modules Menu - appears BELOW Pin button when clicked */}
          {showModulesMenu && (
            <div
              style={{
                position: 'absolute',
                top: '65px', // Below Pin button
                left: '10px', // Aligned with Pin button - top left
                background: 'rgba(255, 255, 255, 0.85)',
                borderRadius: '16px',
                boxShadow: '0 8px 32px rgba(0, 0, 0, 0.18)',
                zIndex: 1001,
                padding: '20px',
                width: '300px',
                backdropFilter: 'blur(12px)',
                border: '1px solid rgba(0, 0, 0, 0.08)',
              }}
            >
              <div
                style={{
                  fontSize: '16px',
                  fontWeight: '700',
                  color: '#1f2937',
                  marginBottom: '14px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '10px',
                  letterSpacing: '-0.01em',
                }}
              >
                {t('map.geointModules')}
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {/* Vision Analysis Module - NEW */}
                <div
                  onClick={() => handleModuleSelect('vision')}
                  style={{
                    padding: '12px',
                    borderRadius: '8px',
                    cursor: 'pointer',
                    border:
                      selectedModule === 'vision'
                        ? '2px solid #8b5cf6'
                        : '1px solid rgba(0, 0, 0, 0.1)',
                    background: selectedModule === 'vision' ? 'rgba(139, 92, 246, 0.1)' : 'white',
                    transition: 'all 0.2s ease',
                  }}
                  onMouseEnter={(e) => {
                    if (selectedModule !== 'vision') {
                      e.currentTarget.style.background = 'rgba(0, 0, 0, 0.05)';
                    }
                  }}
                  onMouseLeave={(e) => {
                    if (selectedModule !== 'vision') {
                      e.currentTarget.style.background = 'white';
                    }
                  }}
                >
                  <div
                    style={{
                      fontSize: '14px',
                      fontWeight: '600',
                      color: '#1f2937',
                      marginBottom: '4px',
                    }}
                  >
                    {t('module.vision')}
                  </div>
                  <div style={{ fontSize: '12px', color: '#6b7280' }}>{t('module.visionDesc')}</div>
                </div>

                {/* Extreme Weather Module */}
                <div
                  onClick={() => handleModuleSelect('extreme_weather')}
                  style={{
                    padding: '12px',
                    borderRadius: '8px',
                    cursor: 'pointer',
                    border:
                      selectedModule === 'extreme_weather'
                        ? '2px solid #f97316'
                        : '1px solid rgba(0, 0, 0, 0.1)',
                    background:
                      selectedModule === 'extreme_weather' ? 'rgba(249, 115, 22, 0.1)' : 'white',
                    transition: 'all 0.2s ease',
                  }}
                  onMouseEnter={(e) => {
                    if (selectedModule !== 'extreme_weather') {
                      e.currentTarget.style.background = 'rgba(0, 0, 0, 0.05)';
                    }
                  }}
                  onMouseLeave={(e) => {
                    if (selectedModule !== 'extreme_weather') {
                      e.currentTarget.style.background = 'white';
                    }
                  }}
                >
                  <div
                    style={{
                      fontSize: '14px',
                      fontWeight: '600',
                      color: '#1f2937',
                      marginBottom: '4px',
                    }}
                  >
                    {t('module.weather')}
                  </div>
                  <div style={{ fontSize: '12px', color: '#6b7280' }}>
                    {t('module.weatherDesc')}
                  </div>
                </div>

                {/* Terrain Analysis Module */}
                <div
                  onClick={() => handleModuleSelect('terrain')}
                  style={{
                    padding: '12px',
                    borderRadius: '8px',
                    cursor: 'pointer',
                    border:
                      selectedModule === 'terrain'
                        ? '2px solid #3b82f6'
                        : '1px solid rgba(0, 0, 0, 0.1)',
                    background: selectedModule === 'terrain' ? 'rgba(59, 130, 246, 0.1)' : 'white',
                    transition: 'all 0.2s ease',
                  }}
                  onMouseEnter={(e) => {
                    if (selectedModule !== 'terrain') {
                      e.currentTarget.style.background = 'rgba(0, 0, 0, 0.05)';
                    }
                  }}
                  onMouseLeave={(e) => {
                    if (selectedModule !== 'terrain') {
                      e.currentTarget.style.background = 'white';
                    }
                  }}
                >
                  <div
                    style={{
                      fontSize: '14px',
                      fontWeight: '600',
                      color: '#1f2937',
                      marginBottom: '4px',
                    }}
                  >
                    {t('module.terrain')}
                  </div>
                  <div style={{ fontSize: '12px', color: '#6b7280' }}>
                    {t('module.terrainDesc')}
                  </div>
                </div>

                {/* Mobility Analysis Module */}
                <div
                  onClick={() => handleModuleSelect('mobility')}
                  style={{
                    padding: '12px',
                    borderRadius: '8px',
                    cursor: 'pointer',
                    border:
                      selectedModule === 'mobility'
                        ? '2px solid #10b981'
                        : '1px solid rgba(0, 0, 0, 0.1)',
                    background: selectedModule === 'mobility' ? 'rgba(16, 185, 129, 0.1)' : 'white',
                    transition: 'all 0.2s ease',
                  }}
                  onMouseEnter={(e) => {
                    if (selectedModule !== 'mobility') {
                      e.currentTarget.style.background = 'rgba(0, 0, 0, 0.05)';
                    }
                  }}
                  onMouseLeave={(e) => {
                    if (selectedModule !== 'mobility') {
                      e.currentTarget.style.background = 'white';
                    }
                  }}
                >
                  <div
                    style={{
                      fontSize: '14px',
                      fontWeight: '600',
                      color: '#1f2937',
                      marginBottom: '4px',
                    }}
                  >
                    {t('module.mobility')}
                  </div>
                  <div style={{ fontSize: '12px', color: '#6b7280' }}>
                    {t('module.mobilityDesc')}
                  </div>
                </div>

                {/* Building Damage Analysis Module */}
                <div
                  onClick={() => handleModuleSelect('building_damage')}
                  style={{
                    padding: '12px',
                    borderRadius: '8px',
                    cursor: 'pointer',
                    border:
                      selectedModule === 'building_damage'
                        ? '2px solid #ef4444'
                        : '1px solid rgba(0, 0, 0, 0.1)',
                    background:
                      selectedModule === 'building_damage' ? 'rgba(239, 68, 68, 0.1)' : 'white',
                    transition: 'all 0.2s ease',
                  }}
                  onMouseEnter={(e) => {
                    if (selectedModule !== 'building_damage') {
                      e.currentTarget.style.background = 'rgba(0, 0, 0, 0.05)';
                    }
                  }}
                  onMouseLeave={(e) => {
                    if (selectedModule !== 'building_damage') {
                      e.currentTarget.style.background = 'white';
                    }
                  }}
                >
                  <div
                    style={{
                      fontSize: '14px',
                      fontWeight: '600',
                      color: '#1f2937',
                      marginBottom: '4px',
                    }}
                  >
                    {t('module.damage')}
                  </div>
                  <div style={{ fontSize: '12px', color: '#6b7280' }}>{t('module.damageDesc')}</div>
                </div>

                {/* Comparison Module */}
                <div
                  onClick={() => handleModuleSelect('comparison')}
                  style={{
                    padding: '12px',
                    borderRadius: '8px',
                    cursor: 'pointer',
                    border:
                      selectedModule === 'comparison'
                        ? '2px solid #f59e0b'
                        : '1px solid rgba(0, 0, 0, 0.1)',
                    background:
                      selectedModule === 'comparison' ? 'rgba(245, 158, 11, 0.1)' : 'white',
                    transition: 'all 0.2s ease',
                  }}
                  onMouseEnter={(e) => {
                    if (selectedModule !== 'comparison') {
                      e.currentTarget.style.background = 'rgba(0, 0, 0, 0.05)';
                    }
                  }}
                  onMouseLeave={(e) => {
                    if (selectedModule !== 'comparison') {
                      e.currentTarget.style.background = 'white';
                    }
                  }}
                >
                  <div
                    style={{
                      fontSize: '14px',
                      fontWeight: '600',
                      color: '#1f2937',
                      marginBottom: '4px',
                    }}
                  >
                    {t('module.comparison')}
                  </div>
                  <div style={{ fontSize: '12px', color: '#6b7280' }}>
                    {t('module.comparisonDesc')}
                  </div>
                </div>
              </div>

              {selectedModule && (
                <div
                  style={{
                    marginTop: '14px',
                    padding: '10px 14px',
                    background: 'rgba(34, 197, 94, 0.12)',
                    borderRadius: '8px',
                    fontSize: '13px',
                    color: '#059669',
                    fontWeight: '600',
                    textAlign: 'center',
                  }}
                >
                  {selectedModule === 'vision'
                    ? 'Vision Analysis'
                    : selectedModule === 'terrain'
                      ? 'Terrain Analysis'
                      : selectedModule === 'mobility'
                        ? 'Mobility Analysis'
                        : selectedModule === 'building_damage'
                          ? 'Building Damage'
                          : selectedModule === 'extreme_weather'
                            ? 'Extreme Weather'
                            : 'Comparison'}{' '}
                  selected
                </div>
              )}
            </div>
          )}

          {/* Before/After Toggle Buttons - only show in comparison mode with imagery */}
          {comparisonMode && comparisonState.beforeImagery && comparisonState.afterImagery && (
            <div
              style={{
                position: 'absolute',
                top: '10px',
                right: '10px',
                display: 'flex',
                gap: '8px',
                zIndex: 1000,
              }}
            >
              <div
                onClick={() => {
                  if (!comparisonState.showingBefore) toggleBeforeAfter();
                }}
                style={{
                  padding: '10px 20px',
                  borderRadius: '8px',
                  fontSize: '13px',
                  fontWeight: '600',
                  cursor: 'pointer',
                  border: comparisonState.showingBefore
                    ? '2px solid #3b82f6'
                    : '1px solid rgba(0, 0, 0, 0.1)',
                  background: comparisonState.showingBefore
                    ? 'rgba(59, 130, 246, 0.95)'
                    : 'rgba(255, 255, 255, 0.9)',
                  color: comparisonState.showingBefore ? 'white' : '#1f2937',
                  transition: 'all 0.2s ease',
                  backdropFilter: 'blur(10px)',
                  userSelect: 'none',
                }}
                onMouseEnter={(e) => {
                  if (!comparisonState.showingBefore)
                    e.currentTarget.style.background = 'rgba(0, 0, 0, 0.05)';
                }}
                onMouseLeave={(e) => {
                  if (!comparisonState.showingBefore)
                    e.currentTarget.style.background = 'rgba(255, 255, 255, 0.9)';
                }}
              >
                {t('map.before')}
              </div>

              <div
                onClick={() => {
                  if (comparisonState.showingBefore) toggleBeforeAfter();
                }}
                style={{
                  padding: '10px 20px',
                  borderRadius: '8px',
                  fontSize: '13px',
                  fontWeight: '600',
                  cursor: 'pointer',
                  border: !comparisonState.showingBefore
                    ? '2px solid #10b981'
                    : '1px solid rgba(0, 0, 0, 0.1)',
                  background: !comparisonState.showingBefore
                    ? 'rgba(16, 185, 129, 0.95)'
                    : 'rgba(255, 255, 255, 0.9)',
                  color: !comparisonState.showingBefore ? 'white' : '#1f2937',
                  transition: 'all 0.2s ease',
                  backdropFilter: 'blur(10px)',
                  userSelect: 'none',
                }}
                onMouseEnter={(e) => {
                  if (comparisonState.showingBefore)
                    e.currentTarget.style.background = 'rgba(0, 0, 0, 0.05)';
                }}
                onMouseLeave={(e) => {
                  if (comparisonState.showingBefore)
                    e.currentTarget.style.background = 'rgba(255, 255, 255, 0.9)';
                }}
              >
                {t('map.after')}
              </div>
            </div>
          )}

          {/* Zoom In Button */}
          <div
            onClick={handleZoomIn}
            title={t('map.zoomIn')}
            style={{
              position: 'absolute',
              top: '126px', // Under map style button
              left: '10px',
              background: 'rgba(255, 255, 255, 0.3)',
              color: '#1a1a1a',
              width: '48px',
              height: '48px',
              borderRadius: '12px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '24px',
              boxShadow: '0 2px 12px rgba(0, 0, 0, 0.15)',
              zIndex: 1000,
              border: '1px solid rgba(0, 0, 0, 0.15)',
              cursor: 'pointer',
              userSelect: 'none',
              transition: 'all 0.2s ease',
              backdropFilter: 'blur(10px)',
              fontWeight: '300',
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.transform = 'scale(1.08)';
              e.currentTarget.style.boxShadow = '0 4px 16px rgba(0, 0, 0, 0.2)';
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.transform = 'scale(1)';
              e.currentTarget.style.boxShadow = '0 2px 12px rgba(0, 0, 0, 0.15)';
            }}
          >
            +
          </div>

          {/* Zoom Out Button - positioned under zoom in button */}
          <div
            onClick={handleZoomOut}
            title={t('map.zoomOut')}
            style={{
              position: 'absolute',
              top: '184px', // Under zoom in button
              left: '10px',
              background: 'rgba(255, 255, 255, 0.3)',
              color: '#1a1a1a',
              width: '48px',
              height: '48px',
              borderRadius: '12px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '24px',
              boxShadow: '0 2px 12px rgba(0, 0, 0, 0.15)',
              zIndex: 1000,
              border: '1px solid rgba(0, 0, 0, 0.15)',
              cursor: 'pointer',
              userSelect: 'none',
              transition: 'all 0.2s ease',
              backdropFilter: 'blur(10px)',
              fontWeight: '300',
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.transform = 'scale(1.08)';
              e.currentTarget.style.boxShadow = '0 4px 16px rgba(0, 0, 0, 0.2)';
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.transform = 'scale(1)';
              e.currentTarget.style.boxShadow = '0 2px 12px rgba(0, 0, 0, 0.15)';
            }}
          >
            −
          </div>

          {/* Compass/Reset Bearing Button - positioned under zoom out button */}
          <div
            onClick={handleResetBearing}
            title={t('map.resetRotation')}
            style={{
              position: 'absolute',
              top: '242px', // Under zoom out button
              left: '10px',
              background: 'rgba(255, 255, 255, 0.3)',
              color: '#1a1a1a',
              width: '48px',
              height: '48px',
              borderRadius: '12px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '20px',
              boxShadow: '0 2px 12px rgba(0, 0, 0, 0.15)',
              zIndex: 1000,
              border: '1px solid rgba(0, 0, 0, 0.15)',
              cursor: 'pointer',
              userSelect: 'none',
              transition: 'all 0.2s ease',
              backdropFilter: 'blur(10px)',
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.transform = 'scale(1.08)';
              e.currentTarget.style.boxShadow = '0 4px 16px rgba(0, 0, 0, 0.2)';
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.transform = 'scale(1)';
              e.currentTarget.style.boxShadow = '0 2px 12px rgba(0, 0, 0, 0.15)';
            }}
          >
            {/* Compass icon */}
            <svg
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <circle cx="12" cy="12" r="10"></circle>
              <polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"></polygon>
            </svg>
          </div>

          {/* Data Catalog Toggle Button - positioned under compass button */}
          <div
            onClick={onToggleSidebar}
            title={sidebarOpen ? 'Collapse Data Catalog' : 'Expand Data Catalog'}
            style={{
              position: 'absolute',
              top: '300px', // Under compass button
              left: '10px',
              background: 'rgba(255, 255, 255, 0.3)',
              color: '#1a1a1a',
              width: '48px',
              height: '48px',
              borderRadius: '12px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '20px',
              boxShadow: '0 2px 12px rgba(0, 0, 0, 0.15)',
              zIndex: 1000,
              border: '1px solid rgba(0, 0, 0, 0.15)',
              cursor: 'pointer',
              userSelect: 'none',
              transition: 'all 0.2s ease',
              backdropFilter: 'blur(10px)',
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.transform = 'scale(1.08)';
              e.currentTarget.style.boxShadow = '0 4px 16px rgba(0, 0, 0, 0.2)';
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.transform = 'scale(1)';
              e.currentTarget.style.boxShadow = '0 2px 12px rgba(0, 0, 0, 0.15)';
            }}
          >
            {/* Arrow icon - rotates based on sidebar state */}
            <svg
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              style={{
                transform: sidebarOpen ? 'rotate(0deg)' : 'rotate(180deg)',
                transition: 'transform 0.3s ease',
              }}
            >
              <path
                d="M15 18L9 12L15 6"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </div>

          {/* Zoom Level Indicator - positioned under data catalog button */}
          <div
            title={t('map.currentZoom')}
            style={{
              position: 'absolute',
              top: '358px', // Under data catalog button
              left: '10px',
              background: 'rgba(255, 255, 255, 0.3)',
              color: '#1a1a1a',
              minWidth: '48px',
              height: '48px',
              borderRadius: '12px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '16px',
              fontWeight: '600',
              boxShadow: '0 2px 12px rgba(0, 0, 0, 0.15)',
              zIndex: 1000,
              border: '1px solid rgba(0, 0, 0, 0.15)',
              userSelect: 'none',
              backdropFilter: 'blur(10px)',
              padding: '0 12px',
              fontFamily: 'monospace',
            }}
          >
            Z{currentZoomLevel}
          </div>

          {/* Pin coordinate indicator - show when pin is active */}
          {pinState.active && (
            <div
              style={{
                position: 'absolute',
                bottom: '60px',
                left: '50%',
                transform: 'translateX(-50%)',
                background: 'rgba(200, 200, 200, 0.25)',
                backdropFilter: 'blur(12px)',
                color: '#1f2937',
                padding: '10px 20px',
                borderRadius: '24px',
                fontSize: '14px',
                fontWeight: '500',
                boxShadow: '0 4px 12px rgba(0, 0, 0, 0.15)',
                zIndex: 1000,
                display: 'flex',
                alignItems: 'center',
                gap: '12px',
                border: '1px solid rgba(255, 255, 255, 0.4)',
                fontFamily:
                  '"Segoe UI", "Segoe UI Variable Text", -apple-system, BlinkMacSystemFont, system-ui, Roboto, Inter, "Helvetica Neue", Arial, "Noto Sans"',
              }}
            >
              <span style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <svg
                  width="18"
                  height="18"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="#3B82F6"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" fill="#3B82F6"></path>
                  <circle cx="12" cy="10" r="3" fill="white"></circle>
                </svg>
                <span style={{ fontWeight: '600' }}>
                  {pinState.lat?.toFixed(6)}°, {pinState.lng?.toFixed(6)}°
                </span>
              </span>
              <span
                onClick={handleClearPin}
                style={{
                  cursor: 'pointer',
                  padding: '2px 6px',
                  borderRadius: '10px',
                  background: 'rgba(255, 255, 255, 0.3)',
                  fontSize: '12px',
                  fontWeight: '500',
                }}
              >
                {t('common.clear')}
              </span>
            </div>
          )}

          {/* Terrain Analysis Pin coordinate indicator - modern translucent style */}
          {terrainAnalysisPin.lat && terrainAnalysisPin.lng && (
            <div
              style={{
                position: 'absolute',
                bottom: '20px',
                left: '50%',
                transform: 'translateX(-50%)',
                background: 'rgba(200, 200, 200, 0.25)',
                backdropFilter: 'blur(12px)',
                color: '#1f2937',
                padding: '10px 20px',
                borderRadius: '24px',
                fontSize: '14px',
                fontWeight: '500',
                boxShadow: '0 4px 12px rgba(0, 0, 0, 0.15)',
                zIndex: 1000,
                display: 'flex',
                alignItems: 'center',
                gap: '12px',
                border: '1px solid rgba(255, 255, 255, 0.4)',
                fontFamily:
                  '"Segoe UI", "Segoe UI Variable Text", -apple-system, BlinkMacSystemFont, system-ui, Roboto, Inter, "Helvetica Neue", Arial, "Noto Sans"',
              }}
            >
              <span style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <svg
                  width="18"
                  height="18"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="#3B82F6"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" fill="#3B82F6"></path>
                  <circle cx="12" cy="10" r="3" fill="white"></circle>
                </svg>
                <span style={{ fontWeight: '600' }}>
                  {terrainAnalysisPin.lat.toFixed(6)}°, {terrainAnalysisPin.lng.toFixed(6)}°
                </span>
              </span>
            </div>
          )}
        </>
      )}

      {/* Tile expansion indicator - show when expanding coverage */}
      {isExpanding && (
        <div
          style={{
            position: 'absolute',
            top: '50%',
            left: '50%',
            transform: 'translate(-50%, -50%)',
            background: 'rgba(173, 216, 230, 0.15)',
            color: '#1e40af',
            padding: '12px 20px',
            borderRadius: '8px',
            fontSize: '14px',
            fontWeight: '500',
            backdropFilter: 'blur(4px)',
            boxShadow: '0 2px 8px rgba(135, 206, 235, 0.2)',
            zIndex: 2000,
            display: 'flex',
            alignItems: 'center',
            gap: '10px',
            border: '1px solid rgba(173, 216, 230, 0.3)',
          }}
        >
          <div
            style={{
              width: '16px',
              height: '16px',
              border: '2px solid transparent',
              borderTop: '2px solid #1e40af',
              borderRadius: '50%',
              animation: 'spin 1s linear infinite',
            }}
          />
          {t('map.adjustingTiles')}
        </div>
      )}

      {/* Dataset info panel when selected - REMOVED */}

      {/* Text Visibility Tip - shown when satellite data is loaded */}
      {showStyleTip && satelliteData && mapLoaded && (
        <div className={`map-style-tip ${showStyleTip ? 'show' : ''}`}>
          ? <strong>{t('map.textHardToRead')}</strong>
          <br />� Use the style control (top-right) to switch to <strong>"Road"</strong> or{' '}
          <strong>"Road Shaded Relief"</strong>
          <br />
          � These styles provide much better text contrast over satellite imagery
          <br />� Or use <strong>"Satellite"</strong> (no labels) for pure imagery view
        </div>
      )}

      {/* Data Legend - shown when any visualizable data is displayed */}
      <DataLegend collection={lastCollection || ''} isVisible={showDataLegend && mapLoaded} />

      {/* Get Started Button - positioned in bottom right */}
    </div>
  );
};

export default MapView;
