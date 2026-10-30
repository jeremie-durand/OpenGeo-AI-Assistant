import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nProvider } from '../../i18n/I18nContext';
import Chat from '../Chat';
import MapView from '../MapView';

const PIN = { lat: 40.7128, lng: -74.006 };
const SECOND_PIN = { lat: 41.5, lng: -73.5 };

type MapViewProps = React.ComponentProps<typeof MapView>;
type ChatProps = React.ComponentProps<typeof Chat>;
type MapHandler = (event: any) => void;

/** Minimal stand-in for the Leaflet global, which index.html loads from a CDN. */
function createFakeLeaflet() {
  const handlers = new Map<string, Set<MapHandler>>();
  const chainable = () => {
    const layer: Record<string, any> = {};
    ['addTo', 'bindPopup', 'openPopup', 'on', 'off', 'setOpacity', 'remove'].forEach((method) => {
      layer[method] = vi.fn(() => layer);
    });
    return layer;
  };
  const map = {
    on: vi.fn((event: string, handler: MapHandler) => {
      if (!handlers.has(event)) handlers.set(event, new Set());
      handlers.get(event)!.add(handler);
    }),
    off: vi.fn((event: string, handler: MapHandler) => {
      handlers.get(event)?.delete(handler);
    }),
    setView: vi.fn(),
    getZoom: vi.fn(() => 12),
    getCenter: vi.fn(() => ({ lat: 52, lng: -71.5 })),
    getBounds: vi.fn(() => ({
      getNorth: () => 53,
      getSouth: () => 51,
      getEast: () => -70,
      getWest: () => -73,
    })),
    addLayer: vi.fn(),
    removeLayer: vi.fn(),
    hasLayer: vi.fn(() => false),
    eachLayer: vi.fn(),
    invalidateSize: vi.fn(),
    fitBounds: vi.fn(),
  };
  const leaflet = {
    map: vi.fn((container: HTMLElement) => {
      const tile = document.createElement('img');
      Object.defineProperty(tile, 'naturalWidth', { value: 256 });
      container.appendChild(tile);
      return map;
    }),
    tileLayer: vi.fn(chainable),
    marker: vi.fn(chainable),
    divIcon: vi.fn(() => ({})),
  };
  const fire = (event: string, payload: unknown = {}) => {
    handlers.get(event)?.forEach((handler) => handler(payload));
  };
  return { leaflet, map, fire };
}

/** jsdom has no layout or canvas, so give the screenshot compositor both. */
function stubScreenshotCapture() {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    left: 0,
    top: 0,
    right: 800,
    bottom: 600,
    width: 800,
    height: 600,
  } as DOMRect);
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    fillRect: vi.fn(),
    drawImage: vi.fn(),
  } as unknown as ReturnType<HTMLCanvasElement['getContext']>);
  vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue(
    `data:image/jpeg;base64,${'A'.repeat(6000)}`
  );
}

describe('GEOINT modules flow', () => {
  let fake: ReturnType<typeof createFakeLeaflet>;
  let onGeointAnalysis: ReturnType<typeof vi.fn<NonNullable<MapViewProps['onGeointAnalysis']>>>;
  let onPinChange: ReturnType<typeof vi.fn<NonNullable<MapViewProps['onPinChange']>>>;
  let onModuleSelected: ReturnType<typeof vi.fn<NonNullable<MapViewProps['onModuleSelected']>>>;
  let onModulesMenuOpen: ReturnType<typeof vi.fn<NonNullable<MapViewProps['onModulesMenuOpen']>>>;

  function renderMap() {
    return render(
      <I18nProvider>
        <MapView
          selectedDataset={null}
          onGeointAnalysis={onGeointAnalysis}
          onPinChange={onPinChange}
          onModuleSelected={onModuleSelected}
          onModulesMenuOpen={onModulesMenuOpen}
        />
      </I18nProvider>
    );
  }

  function openModulesMenu() {
    fireEvent.click(screen.getByTitle('Geointelligence Modules'));
  }

  function selectModule(name: RegExp) {
    openModulesMenu();
    fireEvent.click(screen.getByText(name));
  }

  async function clickMap(point: { lat: number; lng: number }) {
    await act(async () => {
      fake.fire('click', { latlng: point });
    });
  }

  function eventsOfType(type: string) {
    return onGeointAnalysis.mock.calls.map(([event]) => event).filter((e) => e.type === type);
  }

  beforeEach(() => {
    localStorage.setItem('sdss-lang', 'en');
    fake = createFakeLeaflet();
    window.L = fake.leaflet;
    vi.spyOn(console, 'log').mockImplementation(() => {});
    onGeointAnalysis = vi.fn();
    onPinChange = vi.fn();
    onModuleSelected = vi.fn();
    onModulesMenuOpen = vi.fn();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    delete (window as Partial<Window>).L;
  });

  describe('module selection', () => {
    it('opens the modules menu from the pin button', () => {
      renderMap();
      expect(screen.queryByText('Terrain Analysis')).not.toBeInTheDocument();

      openModulesMenu();

      expect(screen.getByText('Terrain Analysis')).toBeInTheDocument();
      expect(onModulesMenuOpen).toHaveBeenCalledTimes(1);
    });

    it.each([
      [/^Terrain Analysis$/, 'terrain', 'Terrain Analysis Selected'],
      [/^Mobility Assessment$/, 'mobility', 'Mobility Assessment Selected'],
    ])('announces the selected module %s', (label, module, message) => {
      renderMap();

      selectModule(label);

      expect(onModuleSelected).toHaveBeenCalledWith(module);
      expect(eventsOfType('module_selected')).toEqual([
        expect.objectContaining({ message: expect.stringContaining(message) }),
      ]);
    });

    it('deselects a module when it is clicked twice', () => {
      renderMap();
      selectModule(/^Terrain Analysis$/);

      selectModule(/^Terrain Analysis$/);

      expect(onModuleSelected).toHaveBeenLastCalledWith(null);
      expect(onPinChange).toHaveBeenLastCalledWith(null);
      expect(eventsOfType('module_deselected')).toHaveLength(1);

      selectModule(/^Terrain Analysis$/);

      expect(onModuleSelected).toHaveBeenLastCalledWith('terrain');
    });
  });

  describe('pin placement', () => {
    it('ignores map clicks until a module is selected', async () => {
      renderMap();

      await clickMap(PIN);

      expect(fake.leaflet.marker).not.toHaveBeenCalled();
      expect(onGeointAnalysis).not.toHaveBeenCalled();
    });

    it('prompts for a terrain question once the pin screenshot is captured', async () => {
      stubScreenshotCapture();
      renderMap();
      selectModule(/^Terrain Analysis$/);

      await clickMap(PIN);

      expect(fake.leaflet.marker).toHaveBeenCalledWith([PIN.lat, PIN.lng], expect.anything());
      await waitFor(() => expect(eventsOfType('terrain_ready')).toHaveLength(1), {
        timeout: 2000,
      });
      expect(eventsOfType('terrain_ready')[0].coordinates).toEqual(PIN);
    });

    it('reports an error when the terrain screenshot cannot be captured', async () => {
      vi.spyOn(console, 'warn').mockImplementation(() => {});
      vi.spyOn(console, 'error').mockImplementation(() => {});
      renderMap();
      selectModule(/^Terrain Analysis$/);

      await clickMap(PIN);

      await waitFor(() => expect(eventsOfType('error')).toHaveLength(1), { timeout: 2000 });
      expect(eventsOfType('error')[0].message).toContain('Failed to capture');
      expect(eventsOfType('terrain_ready')).toHaveLength(0);
    });

    it('collects pin A then pin B for a mobility route', async () => {
      stubScreenshotCapture();
      renderMap();
      selectModule(/^Mobility Assessment$/);

      await clickMap(PIN);
      expect(eventsOfType('info')[0].message).toContain('Pin A set');
      expect(eventsOfType('mobility_ready')).toHaveLength(0);

      await clickMap(SECOND_PIN);

      await waitFor(() => expect(eventsOfType('mobility_ready')).toHaveLength(1));
      expect(eventsOfType('mobility_ready')[0]).toMatchObject({
        coordinates: PIN,
        coordinatesB: SECOND_PIN,
      });
      expect(fake.leaflet.marker).toHaveBeenCalledTimes(2);
    });

    it('requires a close zoom before accepting a building damage pin', async () => {
      renderMap();
      selectModule(/Building Damage/);

      await clickMap(PIN);

      await waitFor(() => expect(eventsOfType('error')).toHaveLength(1));
      expect(eventsOfType('error')[0].message).toContain('Zoom In Required');
      expect(eventsOfType('building_damage_ready')).toHaveLength(0);
    });
  });
});

describe('GEOINT notifications in Chat', () => {
  it('shows a module notification sent by the map', async () => {
    Element.prototype.scrollIntoView = vi.fn();
    localStorage.setItem('sdss-lang', 'en');
    const result: ChatProps['mobilityAnalysisResult'] = {
      type: 'module_selected',
      message: '**Terrain Analysis Selected**\n\nDrop a pin on the location.',
    };

    render(
      <QueryClientProvider client={new QueryClient()}>
        <I18nProvider>
          <Chat
            selectedDataset={null}
            chatMode={true}
            geointMode={false}
            mobilityAnalysisResult={result}
          />
        </I18nProvider>
      </QueryClientProvider>
    );

    expect(await screen.findByText('Terrain Analysis Selected')).toBeInTheDocument();
  });
});
