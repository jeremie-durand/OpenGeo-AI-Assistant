// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.

import React, { useState, useEffect } from 'react';
import './HealthCheckInfo.css';
import { authenticatedFetch } from '../services/authHelper';
import { useT } from '../i18n/I18nContext';

interface HealthCheckData {
  status: string;
  timestamp: string;
  message?: string;
  version?: string;
  basic_checks?: {
    semantic_kernel?: boolean;
    geoint?: boolean;
    llm_base_url?: boolean;
    llm_api_key?: boolean;
    llm_model?: boolean;
  };
  // Backend may return either 'checks' or 'connectivity_tests'
  checks?: {
    llm_client?: { status: string; provider?: string; model?: string };
    planetary_computer?: { status: string };
    private_stac_api?: { status: string; api_url?: string };
  };
  connectivity_tests?: {
    llm_client?: { status: string; provider?: string; model?: string };
    planetary_computer?: { status: string };
    private_stac_api?: { status: string; api_url?: string };
  };
}

/** Check if a service status indicates it's working */
const isServiceOk = (status?: string): boolean => {
  if (!status) return false;
  return ['connected', 'configured', 'healthy', 'ok'].includes(status.toLowerCase());
};

interface HealthCheckInfoProps {
  apiBaseUrl?: string;
}

const HealthCheckInfo: React.FC<HealthCheckInfoProps> = ({
  apiBaseUrl = import.meta.env.BACKEND_URL || 'http://localhost:8000',
}) => {
  const t = useT();
  const [healthData, setHealthData] = useState<HealthCheckData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showTooltip, setShowTooltip] = useState(false);
  const [hasInitialLoad, setHasInitialLoad] = useState(false);

  useEffect(() => {
    fetchHealthCheck();
    // Refresh health check every 30 seconds
    const interval = setInterval(fetchHealthCheck, 30000);
    return () => clearInterval(interval);
  }, [apiBaseUrl]);

  const fetchHealthCheck = async () => {
    try {
      const response = await authenticatedFetch(`${apiBaseUrl}/api/health`);
      const data = await response.json();
      setHealthData(data);
      setError(null);
      setHasInitialLoad(true);
    } catch (err) {
      setError('Unable to connect to backend');
      console.error('Health check failed:', err);
      setHasInitialLoad(true);
    } finally {
      setLoading(false);
    }
  };

  const getStatusColor = (status: string): string => {
    switch (status) {
      case 'healthy':
      case 'connected':
        return '#4CAF50';
      case 'degraded':
        return '#FF9800';
      case 'unhealthy':
      case 'failed':
      case 'error':
        return '#F44336';
      default:
        return '#9E9E9E';
    }
  };

  const getStatusIcon = (status: string): string => {
    switch (status) {
      case 'healthy':
      case 'connected':
        return '✓';
      case 'degraded':
        return '⚠';
      case 'unhealthy':
      case 'failed':
      case 'error':
        return '✗';
      default:
        return 'ℹ️';
    }
  };

  const renderHealthTooltip = () => {
    if (!healthData) {
      return (
        <div className="health-tooltip">
          <div className="health-tooltip-header">
            <span className="health-tooltip-title">{t('health.systemStatus')}</span>
          </div>
          <div className="health-tooltip-content">
            {loading ? (
              <div className="health-status-item">
                <span>{t('health.loading')}</span>
              </div>
            ) : (
              <div className="health-status-item error">
                <span>{error || 'Unable to fetch health status'}</span>
              </div>
            )}
          </div>
        </div>
      );
    }

    return (
      <div className="health-tooltip">
        <div className="health-tooltip-header">
          <span className="health-tooltip-title">{t('health.systemStatus')}</span>
          <span
            className="health-status-badge"
            style={{ backgroundColor: getStatusColor(healthData.status) }}
          >
            {getStatusIcon(healthData.status)} {healthData.status.toUpperCase()}
          </span>
        </div>

        <div className="health-tooltip-content">
          <div className="health-section">
            <div className="health-section-title">{t('health.coreServices')}</div>

            {(() => {
              // Backend may return 'checks' or 'connectivity_tests'
              const svc = healthData.checks || healthData.connectivity_tests || {};
              const aiStatus = svc.llm_client?.status;
              const privateStac = svc.private_stac_api;
              return (
                <>
                  <div className="health-status-item">
                    <span className="health-label">{t('health.aiModel')}</span>
                    <span className={`health-value ${isServiceOk(aiStatus) ? 'success' : 'error'}`}>
                      {isServiceOk(aiStatus) ? 'Connected' : 'Disconnected'}
                    </span>
                  </div>

                  {/* Private STAC API — only show when configured */}
                  {privateStac && privateStac.status !== 'not_configured' && (
                    <div className="health-status-item">
                      <span className="health-label">{t('health.privateStac')}</span>
                      <span
                        className={`health-value ${isServiceOk(privateStac.status) ? 'success' : 'error'}`}
                      >
                        {isServiceOk(privateStac.status) ? 'Connected' : 'Disconnected'}
                      </span>
                    </div>
                  )}

                  <div className="health-status-item">
                    <span className="health-label">{t('health.planetaryComputer')}</span>
                    <span
                      className={`health-value ${isServiceOk(svc.planetary_computer?.status) ? 'success' : 'error'}`}
                    >
                      {isServiceOk(svc.planetary_computer?.status) ? 'Connected' : 'Disconnected'}
                    </span>
                  </div>
                </>
              );
            })()}
          </div>

          <div className="health-section">
            <div className="health-section-title">{t('health.lastCheck')}</div>

            <div className="health-status-item">
              <span className="health-value">
                {new Date(healthData.timestamp).toLocaleString('en-US', {
                  timeZone: 'America/New_York',
                  month: '2-digit',
                  day: '2-digit',
                  year: 'numeric',
                  hour: '2-digit',
                  minute: '2-digit',
                  second: '2-digit',
                  hour12: false,
                })}{' '}
                EST
              </span>
            </div>
          </div>

          <div className="health-info-footer">
            <span style={{ fontSize: '10px', opacity: 0.7 }}>{t('health.hint')}</span>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div
      className="health-check-info"
      onMouseEnter={() => hasInitialLoad && setShowTooltip(true)}
      onMouseLeave={() => setShowTooltip(false)}
    >
      <div
        className="health-info-button"
        style={{
          cursor: 'pointer',
        }}
        title={t('health.title')}
      >
        <span className="health-button-label">{t('health.health')}</span>
      </div>

      {showTooltip && hasInitialLoad && renderHealthTooltip()}
    </div>
  );
};

export default HealthCheckInfo;
