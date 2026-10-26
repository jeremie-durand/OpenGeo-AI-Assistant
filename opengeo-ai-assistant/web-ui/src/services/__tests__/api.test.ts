import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiService } from '../api';

type WindowWithEnv = Window & { ENV?: { VITE_API_KEY?: string } };

describe('apiService.getStacApiCollections', () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockClear();
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ configured: true, collections: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete (window as WindowWithEnv).ENV;
  });

  it('sends X-Api-Key header when window.ENV.VITE_API_KEY is set', async () => {
    (window as WindowWithEnv).ENV = { VITE_API_KEY: 'test-key-123' };

    await apiService.getStacApiCollections();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/api/stac/collections');
    const headers = new Headers(init?.headers);
    expect(headers.get('X-Api-Key')).toBe('test-key-123');
  });
});

describe('apiService.sendChatMessage dashboard fields', () => {
  type ApiInternals = { api: { post: (...args: unknown[]) => Promise<unknown> } };
  const internals = apiService as unknown as ApiInternals;
  const dashboardContext = {
    collection: 'sentinel-2-l2a',
    title: 'Sentinel-2 Level-2A',
    bbox: [-72, 45, -71, 46] as [number, number, number, number],
    scene_count: 12,
    capped: false,
    monthly: [{ month: '2026-03', count: 2 }],
    latest_datetime: null,
  };

  async function sentBody(mapContext: Parameters<typeof apiService.sendChatMessage>[6]) {
    const post = vi.spyOn(internals.api, 'post').mockResolvedValue({ status: 200, data: {} });
    await apiService.sendChatMessage('q', undefined, undefined, [], null, false, mapContext);
    const body = post.mock.calls[0][1] as Record<string, unknown>;
    post.mockRestore();
    return body;
  }

  it('copies dashboard_context into the /api/query body when stats are loaded', async () => {
    const body = await sentBody({ dashboard_context: dashboardContext });
    expect(body.dashboard_context).toEqual(dashboardContext);
  });

  it('omits dashboard_context and search_current_view without a focus', async () => {
    const body = await sentBody({ current_collection: 'sentinel-2-l2a' });
    expect(body).not.toHaveProperty('dashboard_context');
    expect(body).not.toHaveProperty('search_current_view');
    expect(body).not.toHaveProperty('current_view_collection');
  });

  it('sends search_current_view and the collection for a dashboard "Show on map" query', async () => {
    const body = await sentBody({
      search_current_view: true,
      current_view_collection: 'chesapeake-lc-13',
    });
    expect(body.search_current_view).toBe(true);
    expect(body.current_view_collection).toBe('chesapeake-lc-13');
  });
});

describe('apiService.sendChatMessage tile_urls', () => {
  it('sends tile_urls as the tilejson URL strings the backend expects', async () => {
    const internals = apiService as unknown as {
      api: { post: (...args: unknown[]) => Promise<unknown> };
    };
    const post = vi.spyOn(internals.api, 'post').mockResolvedValue({ status: 200, data: {} });
    const url = 'https://pc/api/data/v1/item/tilejson.json?collection=naip&item=a';
    await apiService.sendChatMessage('q', undefined, undefined, [], null, false, {
      tile_urls: [{ tilejson_url: url, item_id: 'a', collection: 'naip' }],
    });
    const body = post.mock.calls[0][1] as Record<string, unknown>;
    post.mockRestore();
    expect(body.tile_urls).toEqual([url]);
  });
});
