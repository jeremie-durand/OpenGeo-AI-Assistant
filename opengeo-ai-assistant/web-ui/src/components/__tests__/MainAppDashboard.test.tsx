import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nProvider } from '../../i18n/I18nContext';
import MainApp from '../MainApp';
import type { DashboardContext } from '../dashboard/types';

type Props = Record<string, any>;
const latest: { map?: Props; dashboard?: Props; chat?: Props } = {};

vi.mock('../MapView', () => ({
  default: (props: Props) => {
    latest.map = props;
    return null;
  },
}));
vi.mock('../Sidebar', () => ({ default: () => null }));
vi.mock('../Chat', () => ({
  default: (props: Props) => {
    latest.chat = props;
    return null;
  },
}));
vi.mock('../dashboard/DashboardPanel', () => ({
  default: (props: Props) => {
    latest.dashboard = props;
    return <div data-testid="focus">{props.focusedId ?? 'none'}</div>;
  },
}));
// Stable like TanStack Query's data, so the focus effect only re-runs on change
const catalog = {
  data: {
    fetched_at: 0,
    collections: [
      { id: 'sentinel-2-l2a', title: 'Sentinel-2 Level-2A' },
      { id: 'landsat-c2-l2', title: 'Landsat Collection 2 Level-2' },
      { id: 'naip', title: 'NAIP' },
    ],
  },
};
vi.mock('../dashboard/hooks/useDashboardData', () => ({
  useDashboardCatalog: () => catalog,
}));

const appState = { selectedDataset: null } as any;

function renderMainApp() {
  localStorage.setItem('sdss-lang', 'en');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <MainApp
          appState={appState}
          onDatasetSelect={() => undefined}
          onReturnToLanding={() => undefined}
          onRestartSession={() => undefined}
          geointMode={false}
          onGeointToggle={() => undefined}
        />
      </I18nProvider>
    </QueryClientProvider>
  );
}

const showCollection = (id: string) =>
  act(() => latest.map!.onMapContextChange({ current_collection: id }));
const clickDataset = (id: string) => act(() => latest.dashboard!.onFocus(id));
const focus = () => screen.getByTestId('focus').textContent;

describe('MainApp dashboard focus', () => {
  beforeEach(() => {
    latest.map = latest.dashboard = latest.chat = undefined;
  });

  it('follows the chat, a later click, then a later chat collection', () => {
    renderMainApp();
    showCollection('sentinel-2-l2a');
    expect(focus()).toBe('sentinel-2-l2a');

    clickDataset('naip');
    expect(focus()).toBe('naip');

    showCollection('landsat-c2-l2');
    expect(focus()).toBe('landsat-c2-l2');
  });

  it('ignores a collection the dashboard cannot render', () => {
    renderMainApp();
    clickDataset('naip');
    showCollection('some-veda-collection');
    expect(focus()).toBe('naip');
  });

  it('passes dashboard_context to the chat only once the panel reports one', () => {
    renderMainApp();
    showCollection('sentinel-2-l2a');
    expect(latest.chat!.mapContext).not.toHaveProperty('dashboard_context');

    const context = { collection: 'sentinel-2-l2a', title: 'Sentinel-2' } as DashboardContext;
    act(() => latest.dashboard!.onContextChange(context));
    expect(latest.chat!.mapContext).toMatchObject({
      current_collection: 'sentinel-2-l2a',
      dashboard_context: context,
    });

    act(() => latest.dashboard!.onContextChange(null));
    expect(latest.chat!.mapContext).not.toHaveProperty('dashboard_context');
  });

  it('turns "Show on map" into a chat trigger and disables it while chat is busy', () => {
    renderMainApp();
    act(() => latest.dashboard!.onShowOnMap({ id: 'naip', title: 'NAIP' }));
    expect(latest.chat!.dashboardQueryTrigger.query).toContain('NAIP');
    expect(latest.chat!.dashboardQueryTrigger.collection).toBe('naip');

    act(() => latest.chat!.onLoadingChange(true));
    expect(latest.dashboard!.showOnMapDisabled).toBe(true);
  });

  it('starts collapsed below 1440 px and toggles open', () => {
    renderMainApp();
    expect(latest.dashboard!.collapsed).toBe(true);
    act(() => latest.dashboard!.onToggleCollapsed());
    expect(latest.dashboard!.collapsed).toBe(false);
    expect(document.documentElement.style.getPropertyValue('--dashboard-width')).toBe('340px');
  });
});
