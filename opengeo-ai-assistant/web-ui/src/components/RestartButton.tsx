// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.

import React from 'react';
import './RestartButton.css';
import { useT } from '../i18n/I18nContext';

interface RestartButtonProps {
  onRestart: () => void;
}

const RestartButton: React.FC<RestartButtonProps> = ({ onRestart }) => {
  const t = useT();
  return (
    <div className="restart-button" onClick={onRestart} title={t('restart.title')}>
      <span className="restart-button-label">{t('restart.label')}</span>
    </div>
  );
};

export default RestartButton;
