// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.

declare const atlas: any;
import React, { useEffect, useRef } from 'react';
import type { CollectionInfo } from './App';

export default function MapPanel({ geojson, selected }: { geojson: any | null; selected: CollectionInfo | null }) {
  const mapDivRef = useRef<any>(null);
  const leafletMapRef = useRef<any>(null);
  const geoJsonLayerRef = useRef<any>(null);

  // Initialize Leaflet map
  useEffect(() => {
    if (!mapDivRef.current || leafletMapRef.current) return;
    if (typeof window !== 'undefined' && window.L) {
      const map = window.L.map(mapDivRef.current, {
        center: [37.0902, -95.7129],
        zoom: 3,
        zoomControl: true
      });
      window.L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
        attribution: '© Esri World Imagery',
        maxZoom: 22
      }).addTo(map);
      leafletMapRef.current = map;
    }
  }, []);

  // Update GeoJSON overlay
  useEffect(() => {
    if (!leafletMapRef.current) return;
    const map = leafletMapRef.current;
    // Remove previous GeoJSON layer
    if (geoJsonLayerRef.current) {
      map.removeLayer(geoJsonLayerRef.current);
      geoJsonLayerRef.current = null;
    }
    if (geojson && geojson.features && geojson.features.length > 0) {
      const geoLayer = window.L.geoJSON(geojson, {
        style: {
          color: 'orange',
          weight: 2,
          fillColor: 'rgba(255,165,0,0.2)',
          fillOpacity: 0.7
        }
      }).addTo(map);
      geoJsonLayerRef.current = geoLayer;
      // Fit map to features
      const bounds = geoLayer.getBounds();
      if (bounds.isValid()) {
        map.fitBounds(bounds, { padding: [50, 50] });
      }
    }
  }, [geojson]);

  // Fit map to selected collection extent
  useEffect(() => {
    if (selected?.extent_bbox && leafletMapRef.current) {
      const b = selected.extent_bbox;
      leafletMapRef.current.fitBounds([[b[1], b[0]], [b[3], b[2]]], { padding: [50, 50] });
    }
  }, [selected]);

  return <div ref={mapDivRef} style={{ width: '100%', height: '100%' }} />;
}
