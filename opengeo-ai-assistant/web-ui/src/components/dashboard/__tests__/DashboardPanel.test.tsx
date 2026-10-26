import React from 'react';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { apiService } from '../../../services/api';
import DashboardPanel from '../DashboardPanel';
import { renderWithI18n } from './renderWithI18n';

const CATALOG = {
  fetched_at: 1,
  collections: [
    {
      id: 'sentinel-2-l2a',
      title: 'Sentinel-2 Level-2A',
      category: 'Optical Satellite Imagery',
      bbox: [-180, -90, 180, 90] as [number, number, number, number],
      is_global: true,
    },
  ],
};
const SHERBROOKE = { west: -72.0, south: 45.3, east: -71.8, north: 45.5 };

function renderPanel(lang: 'en' | 'fr' = 'en', focusedId: string | null = null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onContextChange = vi.fn();
  renderWithI18n(
    <QueryClientProvider client={client}>
      <DashboardPanel
        mapBounds={SHERBROOKE}
        focusedId={focusedId}
        onFocus={vi.fn()}
        onShowOnMap={vi.fn()}
        showOnMapDisabled={false}
        onContextChange={onContextChange}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />
    </QueryClientProvider>,
    lang
  );
  return { onContextChange };
}

beforeEach(() => {
  vi.spyOn(apiService, 'getDashboardCollections').mockResolvedValue(CATALOG);
});

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe('DashboardPanel', () => {
  it('shows category counts for the settled view', async () => {
    renderPanel();
    expect(await screen.findByText('Optical Satellite Imagery')).toBeInTheDocument();
    expect(screen.getByText('1')).toBeInTheDocument();
  });

  it('renders its labels in French when French is selected', async () => {
    renderPanel('fr');
    expect(screen.getByRole('heading', { name: 'Tableau de bord' })).toBeInTheDocument();
    expect(await screen.findByText('Jeux de données dans la vue')).toBeInTheDocument();
    expect(screen.getByText(/Choisissez un jeu de données/)).toBeInTheDocument();
  });

  it('offers a retry when the catalog fails to load', async () => {
    vi.spyOn(apiService, 'getDashboardCollections').mockRejectedValue(new Error('503'));
    renderPanel();
    expect(
      await screen.findByRole('button', { name: 'Retry' }, { timeout: 3000 })
    ).toBeInTheDocument();
  });

  it('reports the focused stats as chat context', async () => {
    vi.spyOn(apiService, 'getDashboardStats').mockResolvedValue({
      collection: 'sentinel-2-l2a',
      bbox: [-72, 45.3, -71.8, 45.5],
      scene_count: 12,
      capped: false,
      static: false,
      latest_datetime: '2026-10-21T15:53:09Z',
      monthly: [{ month: '2026-10', count: 12 }],
    });
    const { onContextChange } = renderPanel('en', 'sentinel-2-l2a');
    await waitFor(() =>
      expect(onContextChange).toHaveBeenLastCalledWith(
        expect.objectContaining({
          collection: 'sentinel-2-l2a',
          title: 'Sentinel-2 Level-2A',
          scene_count: 12,
        })
      )
    );
    await act(async () => {});
  });
});
