// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.

import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { I18nProvider } from './i18n/I18nContext';
import 'modern-normalize/modern-normalize.css';

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <I18nProvider>
      <App />
    </I18nProvider>
  </React.StrictMode>
);
