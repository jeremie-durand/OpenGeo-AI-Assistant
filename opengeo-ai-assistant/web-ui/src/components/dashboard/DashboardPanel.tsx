import React, { useEffect, useMemo } from 'react';
import { useT } from '../../i18n/I18nContext';
import CategorySummary from './CategorySummary';
import FocusedStats from './FocusedStats';
import { useSettledViewport } from './hooks/useSettledViewport';
import { useViewportSummary } from './hooks/useViewportSummary';
import { httpStatus, useCollectionStats, useDashboardCatalog } from './hooks/useDashboardData';
import type { MapBounds } from './lib/bbox';
import type { CatalogCollection, DashboardContext } from './types';
import './DashboardPanel.css';

interface DashboardPanelProps {
  mapBounds: MapBounds | null | undefined;
  focusedId: string | null;
  onFocus: (collectionId: string) => void;
  onShowOnMap: (collection: CatalogCollection) => void;
  showOnMapDisabled: boolean;
  /** Called with the context to send to the chat, or null when there is none. */
  onContextChange: (context: DashboardContext | null) => void;
  collapsed: boolean;
  onToggleCollapsed: () => void;
}

/** Datasets covering the map view, and imagery statistics of the focused one. */
const DashboardPanel: React.FC<DashboardPanelProps> = ({
  mapBounds,
  focusedId,
  onFocus,
  onShowOnMap,
  showOnMapDisabled,
  onContextChange,
  collapsed,
  onToggleCollapsed,
}) => {
  const t = useT();
  const catalog = useDashboardCatalog();
  const viewport = useSettledViewport(mapBounds);
  const summary = useViewportSummary(catalog.data?.collections, viewport);
  const stats = useCollectionStats(focusedId, viewport);
  const focused = useMemo(
    () => catalog.data?.collections.find((c) => c.id === focusedId),
    [catalog.data, focusedId]
  );

  useEffect(() => {
    const data = stats.data;
    if (!focused || !data || data.collection !== focused.id) {
      onContextChange(null);
      return;
    }
    onContextChange({
      collection: data.collection,
      title: focused.title,
      bbox: data.bbox,
      scene_count: data.scene_count,
      capped: data.capped,
      monthly: data.monthly ?? [],
      cloud_cover: data.cloud_cover,
      latest_datetime: data.latest_datetime,
    });
  }, [focused, stats.data, onContextChange]);

  if (collapsed) {
    return (
      <aside className="dashboard-panel is-collapsed">
        <button
          type="button"
          className="dashboard-toggle"
          onClick={onToggleCollapsed}
          aria-label={t('dashboard.expand')}
          title={t('dashboard.expand')}
        >
          ‹
        </button>
      </aside>
    );
  }

  return (
    <aside className="dashboard-panel" aria-label={t('dashboard.title')}>
      <header className="dashboard-header">
        <h2>{t('dashboard.title')}</h2>
        <button
          type="button"
          className="dashboard-toggle"
          onClick={onToggleCollapsed}
          aria-label={t('dashboard.collapse')}
          title={t('dashboard.collapse')}
        >
          ›
        </button>
      </header>

      <section className="dashboard-section">
        <h3>{t('dashboard.datasetsInView')}</h3>
        {catalog.isError ? (
          <div className="dashboard-message" role="alert">
            <p>{t('dashboard.catalogError')}</p>
            <button type="button" onClick={() => catalog.refetch()}>
              {t('dashboard.retry')}
            </button>
          </div>
        ) : catalog.isLoading ? (
          <p className="dashboard-message">{t('dashboard.loadingCatalog')}</p>
        ) : !viewport ? (
          <p className="dashboard-message">{t('dashboard.waitingForMap')}</p>
        ) : summary.length === 0 ? (
          <p className="dashboard-message">{t('dashboard.noDatasets')}</p>
        ) : (
          <CategorySummary
            entries={summary}
            focusedId={focusedId}
            onFocus={(collection) => onFocus(collection.id)}
            onShowOnMap={onShowOnMap}
            showOnMapDisabled={showOnMapDisabled}
          />
        )}
      </section>

      <section className="dashboard-section">
        {focused ? (
          <FocusedStats
            title={focused.title}
            stats={stats.data}
            isLoading={stats.isLoading || !viewport}
            isError={stats.isError}
            errorStatus={httpStatus(stats.error)}
          />
        ) : (
          <p className="dashboard-message">{t('dashboard.pickDataset')}</p>
        )}
      </section>
    </aside>
  );
};

export default DashboardPanel;
