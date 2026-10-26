import React from 'react';
import { render } from '@testing-library/react';
import { I18nProvider } from '../../../i18n/I18nContext';

/** Render inside the i18n provider, optionally in a given language. */
export function renderWithI18n(ui: React.ReactElement, lang: 'en' | 'fr' = 'en') {
  localStorage.setItem('sdss-lang', lang);
  return render(<I18nProvider>{ui}</I18nProvider>);
}
