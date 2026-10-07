import { useEffect, useState } from 'react';

type Theme = 'light' | 'dark';
const key = 'bts.theme';

function readTheme(): Theme {
  try {
    const stored = window.localStorage.getItem(key);
    if (stored === 'light' || stored === 'dark') return stored;
  } catch { /* The toggle also works when storage is unavailable. */ }
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

const initial = readTheme();
document.documentElement.dataset.theme = initial;

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(initial);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { window.localStorage.setItem(key, theme); } catch { /* Keep the in-memory choice. */ }
  }, [theme]);
  return [theme, () => setTheme(value => value === 'dark' ? 'light' : 'dark')] as const;
}
