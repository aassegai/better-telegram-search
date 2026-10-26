import type { CSSProperties } from 'react';

const paths = {
  archive: <><path d="M4 7h16v14H4zM3 3h18v4H3z" /><path d="M9 12h6" /></>,
  settings: <><path d="m9 2-1 3-2 1-3-1-2 4 2 2v2l-2 2 2 4 3-1 2 1 1 3h6l1-3 2-1 3 1 2-4-2-2v-2l2-2-2-4-3 1-2-1-1-3z" /><circle cx="12" cy="12" r="3" /></>,
  plus: <path d="M12 5v14M5 12h14" />,
  close: <path d="m6 6 12 12M6 18 18 6" />,
  search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></>,
  arrow: <path d="M5 12h14m-6-6 6 6-6 6" />,
  left: <path d="M19 12H5m6-6-6 6 6 6" />,
  chevron: <path d="m8 4 8 8-8 8" />,
  more: <><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></>,
  moon: <path d="M20 14a8.5 8.5 0 0 1-10-10A8.5 8.5 0 1 0 20 14Z" />,
  sun: <><circle cx="12" cy="12" r="4" /><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5" /></>,
  calendar: <><rect x="3" y="5" width="18" height="16" rx="2" /><path d="M7 3v4m10-4v4M3 11h18" /></>,
  star: <path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9Z" />,
};
export type IconName = keyof typeof paths;

export default function Icon({ name, size = 20, style }: { name: IconName; size?: number; style?: CSSProperties }) {
  return <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
    aria-hidden="true" focusable="false" style={style}>{paths[name]}</svg>;
}
