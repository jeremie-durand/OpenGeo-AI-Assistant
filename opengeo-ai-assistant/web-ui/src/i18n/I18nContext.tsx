import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { translations } from './translations';

export type Lang = 'fr' | 'en';

/**
 * Language is shared with the host platform (Agri-SDSS) through localStorage.
 * The host nav writes `sdss-lang` and dispatches `sdss-lang-change`; the chat UI
 * is served from the same origin, so both signals reach us directly.
 */
const STORAGE_KEY = 'sdss-lang';
const CHANGE_EVENT = 'sdss-lang-change';
const DEFAULT_LANG: Lang = 'fr';

function isLang(value: unknown): value is Lang {
  return value === 'fr' || value === 'en';
}

export function readLang(): Lang {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return isLang(stored) ? stored : DEFAULT_LANG;
  } catch {
    return DEFAULT_LANG;
  }
}

export type TFunction = (key: string, fallback?: string) => string;

interface I18nValue {
  lang: Lang;
  setLang: (lang: Lang) => void;
  t: TFunction;
}

const I18nContext = createContext<I18nValue | null>(null);

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLangState] = useState<Lang>(readLang);

  useEffect(() => {
    const sync = () => setLangState(readLang());

    const onChange = (event: Event) => {
      const detail = (event as CustomEvent<{ lang?: unknown }>).detail;
      if (detail && isLang(detail.lang)) setLangState(detail.lang);
      else sync();
    };

    // Same tab: host dispatches a CustomEvent. Other tabs: storage event.
    window.addEventListener(CHANGE_EVENT, onChange);
    window.addEventListener('storage', sync);
    return () => {
      window.removeEventListener(CHANGE_EVENT, onChange);
      window.removeEventListener('storage', sync);
    };
  }, []);

  const setLang = useCallback((next: Lang) => {
    setLangState(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* storage unavailable — keep the in-memory language */
    }
    window.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: { lang: next } }));
  }, []);

  const t = useCallback<TFunction>(
    (key, fallback) => translations[lang][key] ?? fallback ?? key,
    [lang]
  );

  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);

  const value = useMemo<I18nValue>(() => ({ lang, setLang, t }), [lang, setLang, t]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error('useI18n must be used inside an I18nProvider');
  return ctx;
}

/** Convenience hook for components that only need the translate function. */
export function useT(): TFunction {
  return useI18n().t;
}
