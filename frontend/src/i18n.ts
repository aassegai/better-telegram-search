import { useEffect, useSyncExternalStore } from 'react';
import english from './locales/en.json' with { type: 'json' };
import backendEnglish from './locales/backend.en.json' with { type: 'json' };

export type Language = 'ru' | 'en';
type Parameters = Record<string, string | number>;
const storageKey = 'bts.language';
const catalog: Record<string, string> = { ...english, ...backendEnglish };

function readLanguage(): Language {
  try { return window.localStorage.getItem(storageKey) === 'en' ? 'en' : 'ru'; }
  catch { return 'ru'; }
}

let language = readLanguage();
const listeners = new Set<() => void>();
export const getLanguage = () => language;
export const uiLocale = () => language === 'en' ? 'en-US' : 'ru-RU';

export function setLanguage(next: Language) {
  if (next !== 'ru' && next !== 'en') return;
  language = next;
  try { window.localStorage.setItem(storageKey, next); } catch { /* The toggle still works in memory. */ }
  listeners.forEach(listener => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function useLanguage() {
  const current = useSyncExternalStore(subscribe, getLanguage);
  useEffect(() => {
    document.documentElement.lang = current;
    document.title = t('Архив · Telegram Search');
  }, [current]);
  return [current, setLanguage] as const;
}

function format(template: string, params: Parameters) {
  // One pass keeps placeholder-like content in paths/names literal.
  return template.replace(/\{(p\d+)\}/gu, (token, name: string) =>
    Object.hasOwn(params, name) ? String(params[name]) : token);
}

function templateMatcher(template: string) {
  const names = [...template.matchAll(/\{(p\d+)\}/gu)].map(match => match[1]);
  const parts = template.split(/\{p\d+\}/u);
  if (!names.length || parts.slice(1, -1).some(part => !part)) return null;
  // Match fixed separators left to right, without regex backtracking over
  // names or paths in an error. Unknown messages remain untouched.
  return (message: string): Parameters | null => {
    if (!message.startsWith(parts[0])) return null;
    let position = parts[0].length;
    const params: Parameters = {};
    for (let index = 0; index < names.length; index++) {
      const separator = parts[index + 1];
      const last = index === names.length - 1;
      if (last && !message.endsWith(separator)) return null;
      const end = last ? message.length - separator.length : message.indexOf(separator, position);
      if (end < position) return null;
      params[names[index]] = message.slice(position, end);
      position = end + separator.length;
    }
    return position === message.length ? params : null;
  };
}

const reversed = new Map(Object.entries(catalog).map(([key, value]) => [value, key]));
const templates = Object.entries(catalog)
  .filter(([key]) => /\{p\d+\}/u.test(key))
  .map(([key, value]) => ({ key, value, ru: templateMatcher(key), en: templateMatcher(value) }));

/** Translate application-owned labels/statuses only; never chat or OCR text. */
export function t(message: string | null | undefined, params?: Parameters): string {
  if (!message) return '';
  const key = message.trim();
  const prefix = message.slice(0, message.length - message.trimStart().length);
  const suffix = message.slice(message.trimEnd().length);
  if (params) return prefix + format(language === 'en' ? catalog[key] ?? key : key, params) + suffix;
  if (Object.hasOwn(catalog, key)) return prefix + (language === 'en' ? catalog[key] : key) + suffix;
  const original = reversed.get(key);
  if (original) return prefix + (language === 'en' ? key : original) + suffix;
  for (const item of templates) {
    const values = item.ru?.(key) ?? item.en?.(key);
    if (values) return prefix + format(language === 'en' ? item.value : item.key, values) + suffix;
  }
  return message;
}
