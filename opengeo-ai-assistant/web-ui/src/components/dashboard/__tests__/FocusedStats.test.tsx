import { describe, it, expect, afterEach } from 'vitest';
import { screen } from '@testing-library/react';
import FocusedStats from '../FocusedStats';
import { renderWithI18n } from './renderWithI18n';
import type { CollectionStats } from '../types';

const MONTHLY = Array.from({ length: 12 }, (_, i) => ({
  month: `2026-${String(i + 1).padStart(2, '0')}`,
  count: i,
}));

function stats(overrides: Partial<CollectionStats> = {}): CollectionStats {
  return {
    collection: 'sentinel-2-l2a',
    bbox: [-72, 45.3, -71.8, 45.5],
    scene_count: 173,
    capped: false,
    static: false,
    latest_datetime: '2026-10-21T15:53:09Z',
    monthly: MONTHLY,
    cloud_cover: [25, 14, 19, 20, 95],
    ...overrides,
  };
}

function renderStats(props: Partial<React.ComponentProps<typeof FocusedStats>> = {}) {
  return renderWithI18n(
    <FocusedStats
      title="Sentinel-2 Level-2A"
      stats={stats()}
      isLoading={false}
      isError={false}
      errorStatus={undefined}
      {...props}
    />
  );
}

afterEach(() => localStorage.clear());

describe('FocusedStats', () => {
  it('shows count, latest date and both charts', () => {
    renderStats();
    expect(screen.getByText('173')).toBeInTheDocument();
    expect(screen.getByText('Oct 21, 2026')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Scenes per month' })).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Scenes by cloud cover' })).toBeInTheDocument();
  });

  it('shows a capped count as 200+', () => {
    renderStats({ stats: stats({ scene_count: 200, capped: true }) });
    expect(screen.getByText('200+')).toBeInTheDocument();
  });

  it('marks the monthly chart as partial when the count is capped', () => {
    renderStats({ stats: stats({ scene_count: 200, capped: true }) });
    expect(screen.getByText(/Partial: only the first 200 scenes/)).toBeInTheDocument();
  });

  it('shows no partial note below the cap', () => {
    renderStats();
    expect(screen.queryByText(/Partial:/)).toBeNull();
  });

  it('explains static datasets instead of a monthly chart', () => {
    renderStats({ stats: stats({ static: true, monthly: undefined, cloud_cover: undefined }) });
    expect(screen.getByText(/Static dataset/)).toBeInTheDocument();
    expect(screen.queryByRole('img')).toBeNull();
  });

  it('shows the rate-limit message on 429', () => {
    renderStats({ stats: undefined, isError: true, errorStatus: 429 });
    expect(screen.getByRole('alert')).toHaveTextContent('temporarily unavailable');
  });

  it('shows a generic error for other failures', () => {
    renderStats({ stats: undefined, isError: true, errorStatus: 502 });
    expect(screen.getByRole('alert')).toHaveTextContent('could not be loaded');
  });

  it('shows a loading state while stats are in flight', () => {
    renderStats({ stats: undefined, isLoading: true });
    expect(screen.getByText('Loading statistics...')).toBeInTheDocument();
  });
});
