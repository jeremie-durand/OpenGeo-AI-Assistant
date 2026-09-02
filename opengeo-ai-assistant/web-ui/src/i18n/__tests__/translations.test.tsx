import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, act, cleanup } from '@testing-library/react';
import { translations } from '../translations';
import { I18nProvider, readLang, useI18n } from '../I18nContext';

const STORAGE_KEY = 'sdss-lang';
const CHANGE_EVENT = 'sdss-lang-change';

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe('translations catalog', () => {
  it('defines the same keys in every language', () => {
    const en = Object.keys(translations.en).sort();
    const fr = Object.keys(translations.fr).sort();
    expect(fr).toEqual(en);
  });

  it('has no empty strings', () => {
    for (const [lang, entries] of Object.entries(translations)) {
      for (const [key, value] of Object.entries(entries)) {
        expect(value.trim(), `${lang}.${key} is empty`).not.toBe('');
      }
    }
  });

  it('translates the chat greeting in both languages', () => {
    expect(translations.en['chat.welcome']).toBeTruthy();
    expect(translations.fr['chat.welcome']).toBeTruthy();
    expect(translations.fr['chat.welcome']).not.toBe(translations.en['chat.welcome']);
  });
});

function Probe() {
  const { lang, t } = useI18n();
  return <span data-testid="probe">{`${lang}:${t('common.close')}`}</span>;
}

function renderProbe() {
  return render(
    <I18nProvider>
      <Probe />
    </I18nProvider>
  );
}

describe('host language contract', () => {
  it('defaults to French when nothing is stored', () => {
    expect(readLang()).toBe('fr');
  });

  it('ignores an unrecognised stored value', () => {
    localStorage.setItem(STORAGE_KEY, 'de');
    expect(readLang()).toBe('fr');
  });

  it('adopts the language the host wrote before mount', () => {
    localStorage.setItem(STORAGE_KEY, 'en');
    renderProbe();
    expect(screen.getByTestId('probe')).toHaveTextContent('en:Close');
  });

  it('follows the host language toggle after mount', () => {
    localStorage.setItem(STORAGE_KEY, 'fr');
    renderProbe();
    expect(screen.getByTestId('probe')).toHaveTextContent('fr:Fermer');

    act(() => {
      localStorage.setItem(STORAGE_KEY, 'en');
      window.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: { lang: 'en' } }));
    });

    expect(screen.getByTestId('probe')).toHaveTextContent('en:Close');
  });

  it('falls back to stored value when the event carries no usable detail', () => {
    localStorage.setItem(STORAGE_KEY, 'fr');
    renderProbe();

    act(() => {
      localStorage.setItem(STORAGE_KEY, 'en');
      window.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: { lang: 'zz' } }));
    });

    expect(screen.getByTestId('probe')).toHaveTextContent('en:Close');
  });

  it('picks up a change made in another tab', () => {
    localStorage.setItem(STORAGE_KEY, 'fr');
    renderProbe();

    act(() => {
      localStorage.setItem(STORAGE_KEY, 'en');
      window.dispatchEvent(new StorageEvent('storage', { key: STORAGE_KEY }));
    });

    expect(screen.getByTestId('probe')).toHaveTextContent('en:Close');
  });
});
