import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { authenticatedFetch } from '../authHelper';

type WindowWithEnv = Window & { ENV?: { VITE_API_KEY?: string } };

describe('authenticatedFetch', () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response('{}'));

  beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete (window as WindowWithEnv).ENV;
  });

  it('attaches X-Api-Key header when window.ENV.VITE_API_KEY is set', async () => {
    (window as WindowWithEnv).ENV = { VITE_API_KEY: 'test-key-123' };

    await authenticatedFetch('/api/sign-mosaic-url', { method: 'POST' });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0];
    const headers = new Headers(init?.headers);
    expect(headers.get('X-Api-Key')).toBe('test-key-123');
  });

  it('preserves caller-provided headers alongside X-Api-Key', async () => {
    (window as WindowWithEnv).ENV = { VITE_API_KEY: 'test-key-123' };

    await authenticatedFetch('/api/sign-mosaic-url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });

    const [, init] = fetchMock.mock.calls[0];
    const headers = new Headers(init?.headers);
    expect(headers.get('Content-Type')).toBe('application/json');
    expect(headers.get('X-Api-Key')).toBe('test-key-123');
  });

  it('omits X-Api-Key header when no key is configured', async () => {
    await authenticatedFetch('/api/sign-mosaic-url');

    const [, init] = fetchMock.mock.calls[0];
    const headers = new Headers(init?.headers);
    expect(headers.get('X-Api-Key')).toBeNull();
  });
});
