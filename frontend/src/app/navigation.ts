/**
 * Shared navigation types and persistence helpers for the unified app shell.
 *
 * Keep navigation state rules in one place so the app shell can be simplified
 * without changing the existing tab keys or browser-storage behaviour.
 */
export type AppTab = 'search' | 'files' | 'shared' | 'activity' | 'storage';

const ACTIVE_TAB_STORAGE_KEY = 'seedflow_active_tab';

export function getInitialAppTab(): AppTab {
  try {
    const saved = window.localStorage.getItem(ACTIVE_TAB_STORAGE_KEY);
    return saved === 'search' ||
      saved === 'files' ||
      saved === 'shared' ||
      saved === 'activity' ||
      saved === 'storage'
      ? saved
      : 'search';
  } catch {
    return 'search';
  }
}
