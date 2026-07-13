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
