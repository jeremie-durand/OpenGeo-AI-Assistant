import React from 'react';
import { useI18n } from '../../i18n/I18nContext';
import BarChart from './charts/BarChart';
import { formatCount, formatDate, monthLabel } from './lib/format';
import type { CollectionStats } from './types';

const CLOUD_LABELS = ['0-20', '20-40', '40-60', '60-80', '80-100'];

interface FocusedStatsProps {
  title: string;
  stats: CollectionStats | undefined;
  isLoading: boolean;
  /** HTTP status of a failed request, if any; 429 means rate limited. */
  errorStatus: number | undefined;
  isError: boolean;
}

/** Scene count, latest date and charts for the focused collection. */
const FocusedStats: React.FC<FocusedStatsProps> = ({
  title,
  stats,
  isLoading,
  errorStatus,
  isError,
}) => {
  const { t, lang } = useI18n();

  if (isError) {
    const key = errorStatus === 429 ? 'dashboard.rateLimited' : 'dashboard.statsError';
    return (
      <p className="dashboard-message" role="alert">
        {t(key)}
      </p>
    );
  }
  if (isLoading || !stats) {
    return <p className="dashboard-message">{t('dashboard.loadingStats')}</p>;
  }

  return (
    <section className="dashboard-stats" aria-label={title}>
      <h3>{title}</h3>
      <dl className="dashboard-figures">
        <div>
          <dt>{stats.static ? t('dashboard.scenes') : t('dashboard.scenes12Months')}</dt>
          <dd>{formatCount(stats.scene_count, stats.capped)}</dd>
        </div>
        <div>
          <dt>{t('dashboard.latest')}</dt>
          <dd>
            {stats.latest_datetime ? formatDate(stats.latest_datetime, lang) : t('dashboard.none')}
          </dd>
        </div>
      </dl>
      {stats.static && <p className="dashboard-message">{t('dashboard.static')}</p>}
      {stats.monthly && (
        <BarChart
          values={stats.monthly.map((m) => m.count)}
          labels={stats.monthly.map((m) => monthLabel(m.month, lang))}
          title={t('dashboard.scenesPerMonth')}
          emptyText={t('dashboard.noScenes')}
          labelEvery={3}
        />
      )}
      {stats.monthly && stats.capped && (
        <p className="dashboard-message">
          {t('dashboard.partialMonthly').replace('{count}', String(stats.scene_count))}
        </p>
      )}
      {stats.cloud_cover && (
        <BarChart
          values={stats.cloud_cover}
          labels={CLOUD_LABELS.map((range) => `${range}%`)}
          title={t('dashboard.cloudCover')}
          emptyText={t('dashboard.noScenes')}
        />
      )}
    </section>
  );
};

export default FocusedStats;
