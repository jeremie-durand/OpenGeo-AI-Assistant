// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.

import React from 'react';
import STACInfoButton from './STACInfoButton';
import HealthCheckInfo from './HealthCheckInfo';
import RestartButton from './RestartButton';
import GetStartedButton from './GetStartedButton';
import ModelSelector from './ModelSelector';
import UserAccountMenu from './UserAccountMenu';
import { API_BASE_URL } from '../config/api';

interface HeaderProps {
  onReturnToLanding: () => void;
  onRestartSession?: () => void;
  onModelChange?: (modelId: string) => void;
  selectedModel?: string;
}

const Header: React.FC<HeaderProps> = ({ onReturnToLanding, onRestartSession, onModelChange, selectedModel }) => {
  return (
    <div className="top-header">
      <div style={{ padding: '0', paddingLeft: '4px' }}>
        <div className="brand" onClick={onReturnToLanding} style={{cursor:'pointer', transition:'opacity 0.2s ease'}}
             onMouseEnter={(e) => (e.target as HTMLElement).style.opacity = '0.8'}
             onMouseLeave={(e) => (e.target as HTMLElement).style.opacity = '1'}>
          <div className="brand-name">Open Geospatial Copilot</div>
        </div>
      </div>
      <div style={{ 
        padding: '0', 
        display: 'flex', 
        justifyContent: 'flex-end', 
        alignItems: 'center', 
        gap: '12px',
        position: 'absolute',
        top: '16px',
        right: '24px',
        zIndex: 1100
      }}>
        <GetStartedButton />
        <ModelSelector onModelChange={onModelChange} selectedModel={selectedModel} apiBaseUrl={API_BASE_URL} />
        <STACInfoButton />
        <HealthCheckInfo apiBaseUrl={API_BASE_URL} />
        {onRestartSession && <RestartButton onRestart={onRestartSession} />}
        <UserAccountMenu />
      </div>
      <style>{`
        @keyframes pulse {
          0%, 100% {
            opacity: 1;
          }
          50% {
            opacity: 0.5;
          }
        }
      `}</style>
    </div>
  );
};

export default Header;
