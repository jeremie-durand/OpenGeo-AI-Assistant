import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nProvider } from '../../i18n/I18nContext';
import Chat from '../Chat';
import { apiService, MapContext } from '../../services/api';

const context: MapContext = {
  current_collection: 'sentinel-2-l2a',
  dashboard_context: {
    collection: 'sentinel-2-l2a',
    title: 'Sentinel-2 Level-2A',
    bbox: [-72, 45, -71, 46],
    scene_count: 12,
    capped: false,
    monthly: [{ month: '2026-03', count: 2 }],
    latest_datetime: '2026-10-01T00:00:00Z',
  },
};

type ChatProps = React.ComponentProps<typeof Chat>;

function renderChat(props: Partial<ChatProps>) {
  localStorage.setItem('sdss-lang', 'en');
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const ui = (extra: Partial<ChatProps>) => (
    <QueryClientProvider client={client}>
      <I18nProvider>
        <Chat selectedDataset={null} chatMode={true} geointMode={false} {...props} {...extra} />
      </I18nProvider>
    </QueryClientProvider>
  );
  const view = render(ui({}));
  return { ...view, rerenderWith: (extra: Partial<ChatProps>) => view.rerender(ui(extra)) };
}

describe('Chat dashboard integration', () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
    vi.restoreAllMocks();
  });

  it('shows the dashboard chip with the focused title', () => {
    renderChat({ mapContext: context });
    expect(screen.getByText(/Sentinel-2 Level-2A/)).toBeInTheDocument();
  });

  it('hides the chip without dashboard context', () => {
    renderChat({ mapContext: { current_collection: 'sentinel-2-l2a' } });
    expect(document.querySelector('.dashboard-chip')).toBeNull();
  });

  it('appends a "Show on map" query and sends it over the current view', async () => {
    const send = vi
      .spyOn(apiService, 'sendChatMessage')
      .mockResolvedValue({ response: 'Here it is.' });
    const onLoadingChange = vi.fn();
    const { rerenderWith } = renderChat({ mapContext: context, onLoadingChange });

    rerenderWith({
      dashboardQueryTrigger: { query: 'First query', collection: 'naip', timestamp: 1 },
    });
    await screen.findByText('Here it is.');
    rerenderWith({
      dashboardQueryTrigger: {
        query: 'Second query',
        collection: 'chesapeake-lc-13',
        timestamp: 2,
      },
    });

    await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
    expect(screen.getByText('First query')).toBeInTheDocument();
    expect(screen.getByText('Second query')).toBeInTheDocument();
    const sentContext = send.mock.calls[1][6] as MapContext;
    expect(sentContext.search_current_view).toBe(true);
    expect(sentContext.current_view_collection).toBe('chesapeake-lc-13');
    await waitFor(() => expect(onLoadingChange).toHaveBeenLastCalledWith(false));
    expect(onLoadingChange).toHaveBeenCalledWith(true);
  });
});
