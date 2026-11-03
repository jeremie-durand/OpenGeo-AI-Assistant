import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, fireEvent } from '@testing-library/react';
import CategorySummary from '../CategorySummary';
import { renderWithI18n } from './renderWithI18n';
import type { CategorySummaryEntry } from '../types';

const ENTRIES: CategorySummaryEntry[] = [
  {
    category: 'Optical Satellite Imagery',
    count: 2,
    collections: [
      {
        id: 'naip',
        title: 'NAIP',
        category: 'Optical',
        bbox: [-170, 10, -50, 80],
        is_global: false,
      },
      {
        id: 'sentinel-2-l2a',
        title: 'Sentinel-2 Level-2A',
        category: 'Optical',
        bbox: [-180, -90, 180, 90],
        is_global: true,
      },
    ],
  },
];

afterEach(() => localStorage.clear());

function setup(showOnMapDisabled = false) {
  const onFocus = vi.fn();
  const onShowOnMap = vi.fn();
  renderWithI18n(
    <CategorySummary
      entries={ENTRIES}
      focusedId={null}
      onFocus={onFocus}
      onShowOnMap={onShowOnMap}
      showOnMapDisabled={showOnMapDisabled}
    />
  );
  return { onFocus, onShowOnMap };
}

describe('CategorySummary', () => {
  it('shows counts first and expands into datasets on click', () => {
    setup();
    expect(screen.queryByText('NAIP')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /Optical Satellite Imagery/ }));
    expect(screen.getByText('NAIP')).toBeInTheDocument();
    expect(screen.getByText('Global')).toBeInTheDocument();
  });

  it('focuses a dataset when it is clicked', () => {
    const { onFocus } = setup();
    fireEvent.click(screen.getByRole('button', { name: /Optical Satellite Imagery/ }));
    fireEvent.click(screen.getByRole('button', { name: 'NAIP' }));
    expect(onFocus).toHaveBeenCalledWith(ENTRIES[0].collections[0]);
  });

  it('calls the show-on-map handler for that dataset', () => {
    const { onShowOnMap } = setup();
    fireEvent.click(screen.getByRole('button', { name: /Optical Satellite Imagery/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Show on map: Sentinel-2 Level-2A' }));
    expect(onShowOnMap).toHaveBeenCalledWith(ENTRIES[0].collections[1]);
  });

  it('disables show-on-map while a chat query runs', () => {
    setup(true);
    fireEvent.click(screen.getByRole('button', { name: /Optical Satellite Imagery/ }));
    expect(screen.getByRole('button', { name: 'Show on map: NAIP' })).toBeDisabled();
  });
});
