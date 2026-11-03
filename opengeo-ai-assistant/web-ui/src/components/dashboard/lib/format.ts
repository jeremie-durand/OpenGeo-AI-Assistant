import type { Lang } from '../../../i18n/I18nContext';

/** "2026-03" → short month name in the interface language ("Mar", "mars"). */
export function monthLabel(month: string, lang: Lang): string {
  const [year, monthIndex] = month.split('-').map(Number);
  const date = new Date(Date.UTC(year, monthIndex - 1, 1));
  return new Intl.DateTimeFormat(lang, { month: 'short', timeZone: 'UTC' }).format(date);
}

export function formatDate(iso: string, lang: Lang): string {
  return new Intl.DateTimeFormat(lang, { dateStyle: 'medium', timeZone: 'UTC' }).format(
    new Date(iso)
  );
}

/** Scene count, shown as a lower bound ("200+") when the search was capped. */
export function formatCount(count: number, capped: boolean): string {
  return capped ? `${count}+` : String(count);
}
