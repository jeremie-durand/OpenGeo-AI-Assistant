import React, { useState } from 'react';
import { useT } from '../../i18n/I18nContext';
import type { CatalogCollection, CategorySummaryEntry } from './types';

interface CategorySummaryProps {
  entries: CategorySummaryEntry[];
  focusedId: string | null;
  onFocus: (collection: CatalogCollection) => void;
  onShowOnMap: (collection: CatalogCollection) => void;
  showOnMapDisabled: boolean;
}

/** Category counts that expand into their datasets; datasets can be focused or mapped. */
const CategorySummary: React.FC<CategorySummaryProps> = ({
  entries,
  focusedId,
  onFocus,
  onShowOnMap,
  showOnMapDisabled,
}) => {
  const t = useT();
  const [expanded, setExpanded] = useState<string | null>(null);

  return (
    <ul className="dashboard-categories">
      {entries.map(({ category, count, collections }) => {
        const isOpen = expanded === category;
        return (
          <li key={category}>
            <button
              type="button"
              className="dashboard-category"
              aria-expanded={isOpen}
              onClick={() => setExpanded(isOpen ? null : category)}
            >
              <span>{category}</span>
              <span className="dashboard-count">{count}</span>
            </button>
            {isOpen && (
              <ul className="dashboard-datasets">
                {collections.map((collection) => (
                  <li
                    key={collection.id}
                    className={collection.id === focusedId ? 'is-focused' : undefined}
                  >
                    <button
                      type="button"
                      className="dashboard-dataset"
                      aria-pressed={collection.id === focusedId}
                      onClick={() => onFocus(collection)}
                    >
                      {collection.title}
                      {collection.is_global && (
                        <span className="dashboard-badge">{t('dashboard.global')}</span>
                      )}
                    </button>
                    <button
                      type="button"
                      className="dashboard-show-on-map"
                      disabled={showOnMapDisabled}
                      onClick={() => onShowOnMap(collection)}
                      aria-label={`${t('dashboard.showOnMap')}: ${collection.title}`}
                    >
                      {t('dashboard.showOnMap')}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </li>
        );
      })}
    </ul>
  );
};

export default CategorySummary;
