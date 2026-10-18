/**
 * SeedFlow - Cloud Torrent & Media Streaming Application
 * Seedr-style webapp with qBittorrent WebAPI v2 orchestration,
 * unlimited server storage, HTTP range streaming, and selective downloads.
 */

// Deployment marker: frontend is deployed from main via Vercel.
import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { getInitialAppTab, type AppTab } from './app/navigation.ts';
import {
  Cloud,
  Download,
  Copy,
  Upload,
  HardDrive,
  Folder,
  File,
  FolderPlus,
  Play,
  Share2,
  History,
  Settings,
  Bell,
  Plus,
  Search,
  Filter,
  Moon,
  ShieldCheck,
  AlertTriangle,
  RefreshCw,
  Trash2,
  Users,
  ChevronRight,
  Sparkles,
  ArrowUpDown,
  ExternalLink,
  Film,
  Music,
  CheckCircle2,
  Loader2,
  MessageSquare
} from 'lucide-react';

import {
  TorrentItem,
  StorageFile,
  StorageFolder,
  UserProfile,
  StorageStats,
  ActivityLog,
  AppNotification,
  CleanupSettings,
  UserPermission
} from './types/index.ts';

import { api, API_BASE } from './api/client.ts';
import { formatBytes, formatQuotaBytes } from './utils/formatters.ts';
import { dispatchBrowserNotification, playNotificationSound } from './utils/notifications.ts';

import { TorrentCard } from './components/TorrentCard.tsx';
import { FileCard } from './components/FileCard.tsx';
import { MediaPlayerModal } from './components/MediaPlayerModal.tsx';
import { AddMagnetModal } from './components/AddMagnetModal.tsx';
import { FilePrioModal } from './components/FilePrioModal.tsx';
import { StorageCleanupModal } from './components/StorageCleanupModal.tsx';
import { FolderShareModal } from './components/FolderShareModal.tsx';
import { NotificationCenter } from './components/NotificationCenter.tsx';
import { ActivityLogView } from './components/ActivityLogView.tsx';
import { CreateFolderModal } from './components/CreateFolderModal.tsx';
import { MoveFileModal } from './components/MoveFileModal.tsx';
import { RenameModal } from './components/RenameModal.tsx';
import { ConfirmDeleteModal } from './components/ConfirmDeleteModal.tsx';
import { TorrentSearchPanel } from './components/TorrentSearchPanel.tsx';

export default function App() {
  // Navigation & Theme
  // Seedr is connected per browser session using the user's Personal Access Token.
  // The PAT is sent only to our backend over HTTPS and is never stored in localStorage.
  const [productWelcomeOpen, setProductWelcomeOpen] = useState(() => {
    try {
      return window.localStorage.getItem('torrent_studio_welcome_seen') !== 'true';
    } catch {
      return true;
    }
  });
  // Seedr connection is optional. Never open the connection dialog automatically
  // on page load/refresh; the user opens it explicitly or reaches a Seedr action.
  const [seedrOnboardingOpen, setSeedrOnboardingOpen] = useState(false);
  const seedrOnboardingSeenKey = 'seedflow_seedr_onboarding_seen';
  const [seedrConnected, setSeedrConnected] = useState(false);
  const [seedrSessionReady, setSeedrSessionReady] = useState(false);
  const [seedrPat, setSeedrPat] = useState('');
  const [seedrPatSubmitting, setSeedrPatSubmitting] = useState(false);
  const [seedrConnectError, setSeedrConnectError] = useState('');

  const markSeedrOnboardingSeen = useCallback(() => {
    try {
      window.localStorage.setItem(seedrOnboardingSeenKey, '1');
    } catch {
      // Storage can be unavailable in private/sandboxed contexts.
    }
    setSeedrOnboardingOpen(false);
  }, []);

  useEffect(() => {
    let stopped = false;
    void api.getSeedrSession()
      .then(session => {
        if (stopped) return;
        setSeedrSessionReady(true);
        if (session.connected) {
          setSeedrConnected(true);
          setSeedrConfigured(true);
        }
      })
      .catch(() => {
        if (!stopped) setSeedrSessionReady(true);
      });

    return () => {
      stopped = true;
    };
  }, [markSeedrOnboardingSeen]);

  const connectSeedrWithPat = useCallback(async () => {
    const pat = seedrPat.trim();
    if (!pat) {
      setSeedrConnectError('Paste your Seedr token.');
      return;
    }

    setSeedrPatSubmitting(true);
    setSeedrConnectError('');
    try {
      const result = await api.connectSeedrPat(pat);
      if (!result.connected) {
        throw new Error('Seedr did not accept the Personal Access Token.');
      }
      setSeedrPat('');
      setSeedrConnected(true);
      setSeedrConfigured(true);
    } catch (error: any) {
      setSeedrConnectError(
        String(error?.message || 'Seedr rejected the Personal Access Token.')
      );
    } finally {
      setSeedrPatSubmitting(false);
    }
  }, [seedrPat]);

  const [activeTab, setActiveTab] = useState<AppTab>(getInitialAppTab);

  // Treat the app's sections as browser-history states. Android back buttons
  // and swipe-back gestures therefore move from Files/other sections to Search
  // instead of immediately leaving the web app. Once already on Search, the
  // next Back is allowed to leave the page normally.
  useEffect(() => {
    const currentState = window.history.state;
    if (!currentState?.torrentStudio) {
      window.history.replaceState(
        { ...(currentState || {}), torrentStudio: true, tab: activeTab },
        '',
        window.location.href
      );
      return;
    }

    if (currentState.tab !== activeTab) {
      window.history.pushState(
        { ...currentState, torrentStudio: true, tab: activeTab },
        '',
        window.location.href
      );
    }
  }, [activeTab]);

  useEffect(() => {
    const handlePopState = () => {
      const stateTab = window.history.state?.torrentStudio
        ? window.history.state.tab
        : null;

      if (activeTab !== 'search' && stateTab === 'search') {
        setActiveTab('search');
      }
    };

    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, [activeTab]);
  type BackgroundMetadataJobRecord = {
    jobId: string;
    hash: string;
    name: string;
    status: string;
    rounds: number;
    startedAt: number;
    updatedAt: number;
    deadlineAt: number;
    elapsedSeconds: number;
    remainingSeconds: number;
    fileCount: number;
    totalSize: number;
    source: string;
    error?: string | null;
  };

  const [backgroundMetadataJob, setBackgroundMetadataJob] = useState<{
    active: boolean;
    title: string;
    message: string;
    ready?: boolean;
    error?: string;
    jobId?: string;
  } | null>(null);
  const [backgroundMetadataJobs, setBackgroundMetadataJobs] = useState<BackgroundMetadataJobRecord[]>([]);
  const [backgroundMetadataLoading, setBackgroundMetadataLoading] = useState(false);

  // Keep background metadata jobs connected to the UI after the selector closes.
  // Once a resolver finishes, the cached metadata is immediately available when
  // the user opens the selector again.
  useEffect(() => {
    const jobId = backgroundMetadataJob?.jobId;
    if (!backgroundMetadataJob?.active || !jobId) return;

    let stopped = false;
    const poll = async () => {
      try {
        const data = await api.getTorrentMetadataStatus(jobId);
        if (stopped) return;

        if (Array.isArray(data?.files) && data.files.length > 0) {
          setBackgroundMetadataJob({
            active: false,
            ready: true,
            title: 'Torrent metadata ready',
            message: `${data.files.length} file${data.files.length === 1 ? '' : 's'} found. Open the selector to choose files.`,
            jobId
          });
          return;
        }

        if (data?.status === 'error') {
          setBackgroundMetadataJob({
            active: false,
            error: String(data?.message || 'Torrent metadata could not be resolved.'),
            title: 'Torrent metadata failed',
            message: String(data?.message || 'Torrent metadata could not be resolved.'),
            jobId
          });
        }
      } catch {
        // The job may still be running or the Render instance may be waking.
        // Keep polling rather than turning a transient status request into a
        // false failure.
      }
    };

    void poll();
    const timer = window.setInterval(() => { void poll(); }, 5000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [backgroundMetadataJob?.active, backgroundMetadataJob?.jobId]);
  const refreshBackgroundMetadataJobs = useCallback(async () => {
    try {
      setBackgroundMetadataLoading(true);
      const data = await api.getTorrentMetadataJobs();
      setBackgroundMetadataJobs(Array.isArray(data?.jobs) ? data.jobs : []);
    } catch {
      // Metadata jobs are best-effort UI state. A transient API/Render wake-up
      // failure must not affect the rest of the Files screen.
    } finally {
      setBackgroundMetadataLoading(false);
    }
  }, []);

  // Restore the server-side metadata queue when the page loads. Keep polling
  // while at least one job is still resolving, then slow down once everything
  // is terminal so the list remains useful without unnecessary requests.
  const hasActiveBackgroundMetadataJobs = backgroundMetadataJobs.some(
    job => job.status === 'queued' || job.status === 'resolving'
  );

  useEffect(() => {
    void refreshBackgroundMetadataJobs();
    if (!hasActiveBackgroundMetadataJobs) return;
    const timer = window.setInterval(
      () => { void refreshBackgroundMetadataJobs(); },
      5000
    );
    return () => window.clearInterval(timer);
  }, [refreshBackgroundMetadataJobs, hasActiveBackgroundMetadataJobs]);

  const [initialSourceUrl, setInitialSourceUrl] = useState('');
  const [initialDescriptorUrl, setInitialDescriptorUrl] = useState('');

  const [theme, setTheme] = useState<'dark' | 'dim'>(() => {
    try {
      const saved = window.localStorage.getItem('seedflow_theme');
      return saved === 'dim' ? 'dim' : 'dark';
    } catch {
      return 'dark';
    }
  });

  // Core Data
  const [torrents, setTorrents] = useState<TorrentItem[]>([]);
  const [files, setFiles] = useState<StorageFile[]>([]);
  const [folders, setFolders] = useState<StorageFolder[]>([]);
  const [users, setUsers] = useState<UserProfile[]>([]);
  const [activeUser, setActiveUser] = useState<UserProfile | null>(null);
  const [storageStats, setStorageStats] = useState<StorageStats | null>(null);
  const activityStorageKey = 'seedflow_activity_logs';
  const [activityLogs, setActivityLogs] = useState<ActivityLog[]>(() => {
    try {
      const raw = window.localStorage.getItem(activityStorageKey);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  });
  const [notifications, setNotifications] = useState<AppNotification[]>([]);
  const [cleanupSettings, setCleanupSettings] = useState<CleanupSettings | null>(null);
  type SeedrNotice = {
    taskId: number | string | null;
    name: string;
    folderName: string;
    folderId: string;
    status: 'waiting' | 'downloading' | 'completed' | 'not_found';
    progress: number;
    downloadUrl: string | null;
    files: Array<{
      id: string;
      name: string;
      size: number;
      folderId: string;
      folderPath: string;
      url: string | null;
      available?: boolean;
    }>;
    seedrReply: string;
  };

  const seedrNoticeStorageKey = 'seedflow_seedr_notice';
  const seedrPrepareWaiters = useRef<Record<string, {
    resolve: (value: { files: Array<{
      id: string;
      streamId?: string;
      name: string;
      size: number;
      folderId: string;
      folderPath: string;
    }> }) => void;
    reject: (reason?: unknown) => void;
  }>>({});
  const [seedrNotice, setSeedrNotice] = useState<SeedrNotice | null>(() => {
    try {
      const raw = window.localStorage.getItem(seedrNoticeStorageKey);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || parsed.taskId == null) return null;      return {        taskId: parsed.taskId,
        name: String(parsed.name || 'Seedr download'),
        folderName: String(parsed.folderName || ''),
        folderId: String(parsed.folderId || ''),
        status: parsed.status === 'completed' ? 'completed' : parsed.status === 'downloading' ? 'downloading' : 'waiting',
        progress: Math.max(0, Math.min(100, Number(parsed.progress) || 0)),
        downloadUrl: typeof parsed.downloadUrl === 'string' ? parsed.downloadUrl : null,
        files: Array.isArray(parsed.files) ? parsed.files : [],
        seedrReply: String(parsed.seedrReply || ''),
      };
    } catch {
      return null;
    }
  });
  const [seedrFiles, setSeedrFiles] = useState<Array<{ id: string; streamId?: string; name: string; size: number; folderId: string; folderPath: string }>>([]);
  const [seedrDeletedFolderIds, setSeedrDeletedFolderIds] = useState<string[]>([]);
  const [seedrLibraryRoot, setSeedrLibraryRoot] = useState<{
    id: string;
    folderId: string;
    name: string;
    path: string;
    filesCount: number;
    totalSize: number;
    folderCount: number;
  } | null>(null);
  const [seedrLibraryFolders, setSeedrLibraryFolders] = useState<Array<{
    id: string;
    folderId: string;
    name: string;
    path: string;
    filesCount: number;
    totalSize: number;
    folderCount: number;
    active?: boolean;
    progress?: number;
  }>>([]);
  const [seedrFolderContentsLoading, setSeedrFolderContentsLoading] = useState(false);
  const [seedrPrefetchLoading, setSeedrPrefetchLoading] = useState(false);
  const [seedrFolderContentsCache, setSeedrFolderContentsCache] = useState<Record<string, Array<{
    id: string;
    streamId?: string;
    name: string;
    size: number;
    folderId: string;
    folderPath: string;
  }>>>({});
  // Track background folder-content requests so clicking a folder while its
  // automatic prefetch is still running reuses the same promise instead of
  // issuing a duplicate API request.
  const seedrFolderContentsRequests = useRef<Record<string, Promise<Array<{
    id: string;
    streamId?: string;
    name: string;
    size: number;
    folderId: string;
    folderPath: string;
  }>>>>({});
  const [seedrConfigured, setSeedrConfigured] = useState(false);
  const [seedrQuota, setSeedrQuota] = useState<{ maxSpace: number; usedSpace: number; remainingSpace: number } | null>(null);
  const [seedrQuotaError, setSeedrQuotaError] = useState<string | null>(null);
  const [seedrLoading, setSeedrLoading] = useState(false);
  const [seedrError, setSeedrError] = useState<string | null>(null);
  const [seedrDeleteNotice, setSeedrDeleteNotice] = useState<string | null>(null);
  const [copiedSeedrFileId, setCopiedSeedrFileId] = useState<string | null>(null);
  const [seedrAddBlockedNotice, setSeedrAddBlockedNotice] = useState<string | null>(null);
  const [seedrInsufficientSpacePrompt, setSeedrInsufficientSpacePrompt] = useState<{
    requiredBytes: number;
    remainingBytes: number;
    magnet: string;
    category: string;
    selectedFiles?: number[];
    manifest?: { index?: number; name: string; size: number; priority: number }[];
    existingHash?: string;
  } | null>(null);
  // Two-stage Seedr playback UX: first show progress while resolving the
  // stream URL, then the player shows its own browser-loading state.
  const [seedrStreamLoadingId, setSeedrStreamLoadingId] = useState<string | null>(null);
  const [isCancellingSeedr, setIsCancellingSeedr] = useState(false);
  const seedrPrepareGenerationRef = useRef(0);
  const [activeSeedrFolderOpen, setActiveSeedrFolderOpen] = useState(false);
  const [selectedSeedrFolderId, setSelectedSeedrFolderId] = useState<string | null>(() => {
    try {
      return window.localStorage.getItem('seedflow_seedr_folder') || null;
    } catch {
      return null;
    }
  });
  const seedrTorrentNamesKey = 'seedflow_seedr_torrent_names';
  const [seedrTorrentNames, setSeedrTorrentNames] = useState<Record<string, string>>(() => {
    try {
      const raw = window.localStorage.getItem(seedrTorrentNamesKey);
      const parsed = raw ? JSON.parse(raw) : {};
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch {
      return {};
    }
  });

  const rememberSeedrTorrentName = useCallback((folderId: string, torrentName: string) => {
    const id = String(folderId || '').trim();
    const name = String(torrentName || '').trim();
    if (!id || !name || name === 'Waiting for Seedr metadata…') return;

    setSeedrTorrentNames(prev => {
      const next = { ...prev, [id]: name };
      try {
        window.localStorage.setItem(seedrTorrentNamesKey, JSON.stringify(next));
      } catch {}
      return next;
    });
  }, []);

  const forgetSeedrFolders = useCallback((folderIds: Array<string | number>) => {
    const removedIds = new Set(
      folderIds.map(id => String(id || '').trim()).filter(Boolean)
    );
    if (removedIds.size === 0) return;

    setSeedrDeletedFolderIds(prev => Array.from(new Set([...prev, ...removedIds])));
    setSeedrFolderContentsCache(prev =>
      Object.fromEntries(
        Object.entries(prev).filter(([folderId]) => !removedIds.has(String(folderId)))
      )
    );
    setSeedrLibraryFolders(prev =>
      prev.filter(folder => !removedIds.has(String(folder.folderId || folder.id || '')))
    );
    setSeedrFiles(prev =>
      prev.filter(file => !removedIds.has(String(file.folderId || '')))
    );
    setSeedrTorrentNames(prev => {
      const next = { ...prev };
      for (const id of removedIds) delete next[id];
      try {
        window.localStorage.setItem(seedrTorrentNamesKey, JSON.stringify(next));
      } catch {}
      return next;
    });
    setSelectedSeedrFolderId(prev => (
      prev && removedIds.has(String(prev)) ? null : prev
    ));
  }, []);

  const toSeedrStorageFile = useCallback((file: {
    id: string;
    streamId?: string;
    name: string;
    size: number;
    folderId: string;
    folderPath: string;
  }): StorageFile => {
    const lower = file.name.toLowerCase();
    const type: StorageFile['type'] =
      /\.(mkv|mp4|m4v|webm|mov|avi|m3u8|ts)$/i.test(lower) ? 'video' :
      /\.(mp3|wav|flac|aac|ogg|m4a)$/i.test(lower) ? 'audio' :
      /\.(zip|rar|7z|tar|gz|bz2)$/i.test(lower) ? 'archive' :
      /\.(pdf|txt|doc|docx|xls|xlsx|ppt|pptx|csv)$/i.test(lower) ? 'document' :
      'other';

    return {
      id: file.id,
      name: file.name,
      streamId: file.streamId || file.id,
      path: (file.folderPath || '/Torrent Studio').replace(/\/$/, '') + '/' + file.name,
      folder: file.folderPath || '/Torrent Studio',
      size: Number(file.size) || 0,
      type,
      mimeType: type === 'video' ? 'video/mp4' : type === 'audio' ? 'audio/mpeg' : 'application/octet-stream',
      createdAt: Date.now(),
      ownerId: 'seedr',
      ownerName: 'Seedr',
      isStreamable: type === 'video' || type === 'audio',
      downloadUrl: '/api/seedr/files/' + encodeURIComponent(file.id) + '/download',
      // The stream URL is resolved only when the user presses Stream.
      // Keep a placeholder here so library rendering never depends on a
      // runtime streamUrl variable.
      streamUrl: '',
    };
  }, []);

  const seedrAllPrefetchedFiles = useMemo(
    () => Object.values(seedrFolderContentsCache).flat(),
    [seedrFolderContentsCache]
  );

  // Active Seedr transfers are shown only in the transfer card above.
  // The initial response contains folder metadata only; file rows are loaded
  // lazily after a folder is opened.
  const seedrFolderGroups = useMemo(() => {
    type SeedrFolderGroup = {
      folderId: string;
      name: string;
      path: string;
      files: Array<{ id: string; name: string; size: number; folderId: string; folderPath: string }>;
      totalSize: number;
      filesCount: number;
      active?: boolean;
      progress?: number;
    };

    const groups: SeedrFolderGroup[] = seedrLibraryFolders.map(folder => {
      const folderId = folder.folderId || folder.id;
      const savedTorrentName = seedrTorrentNames[folderId];
      const torrentName = String(folder.torrentName || savedTorrentName || '').trim();
      return {
      folderId,
      name: torrentName || folder.name,
      path: folder.path,
      files: [],
      totalSize: Number(folder.totalSize) || 0,
      filesCount: Number(folder.filesCount) || 0,
      active: false,
      progress: undefined,
      };
    });

    if (seedrNotice?.taskId != null && seedrNotice.status !== 'completed') {
      const normalizeSeedrText = (value: string) =>
        value.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();

      const actualFolderId =
        seedrNotice.folderId?.trim() ||
        seedrNotice.files.find(file => file.folderId && !file.folderId.startsWith('__'))?.folderId ||
        '';

      const activeName =
        seedrNotice.name?.trim() ||
        'Seedr download';

      const activeNameKey = normalizeSeedrText(activeName);
      let matchingEntry = actualFolderId
        ? groups.find(group => group.folderId === actualFolderId)
        : undefined;

      if (!matchingEntry) {
        matchingEntry = groups.find(group =>
          normalizeSeedrText(group.name) === activeNameKey ||
          normalizeSeedrText(group.path.split('/').filter(Boolean).pop() || '') === activeNameKey
        );
      }

      const activeFileSize = (seedrNotice.files || []).reduce((sum, file) => sum + Number(file.size || 0), 0);
      const activeFileCount = seedrNotice.files?.length || 0;

      if (matchingEntry) {
        matchingEntry.active = true;
        matchingEntry.progress = Math.max(0, Math.min(100, Number(seedrNotice.progress) || 0));
        matchingEntry.name = activeName || matchingEntry.name;
        if (actualFolderId) matchingEntry.folderId = actualFolderId;
        if (activeFileCount > 0 && matchingEntry.filesCount === 0) matchingEntry.filesCount = activeFileCount;
        if (activeFileSize > 0 && matchingEntry.totalSize === 0) matchingEntry.totalSize = activeFileSize;
      } else {
        groups.unshift({
          folderId: actualFolderId || '__active_seedr__',
          name: activeName,
          path: '/Torrent Studio/' + activeName,
          files: [],
          totalSize: activeFileSize,
          filesCount: activeFileCount,
          active: true,
          progress: Math.max(0, Math.min(100, Number(seedrNotice.progress) || 0)),
        });
      }
    }

    return groups.sort((a, b) => {
      if (a.active && !b.active) return -1;
      if (!a.active && b.active) return 1;
      return a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });
    });
  }, [seedrLibraryFolders, seedrNotice, seedrTorrentNames]);

  useEffect(() => {
    if (
      selectedSeedrFolderId !== null &&
      seedrLibraryFolders.length > 0 &&
      !seedrFolderGroups.some(folder => folder.folderId === selectedSeedrFolderId)
    ) {
      setSelectedSeedrFolderId(null);
      setSeedrFiles([]);
    }
  }, [selectedSeedrFolderId, seedrLibraryFolders.length, seedrFolderGroups]);
  // File Explorer State
  const [currentFolder, setCurrentFolder] = useState<string>('/');  const [fileSearch, setFileSearch] = useState<string>('');
  const [fileTypeFilter, setFileTypeFilter] = useState<string>('all');
  const [selectedFileIds, setSelectedFileIds] = useState<string[]>([]);

  // The Files tab is reserved for completed/stored files and folders.
  // Active qBittorrent downloads belong only in the Transfers tab.
  const visibleFiles = useMemo(() => {
    const source = currentFolder === '/' && seedrAllPrefetchedFiles.length > 0
      ? seedrAllPrefetchedFiles.map(toSeedrStorageFile)
      : files;

    const search = fileSearch.trim().toLowerCase();
    const filtered = source.filter(file => {
      const matchesType = fileTypeFilter === 'all' || file.type === fileTypeFilter;
      const matchesSearch = !search || file.name.toLowerCase().includes(search);
      return matchesType && matchesSearch;
    });

    // Keep root/"outside folder" files deterministic and easy to scan.
    if (currentFolder !== '/' || seedrAllPrefetchedFiles.length === 0) return filtered;

    return [...filtered].sort((a, b) =>
      a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' })
    );
  }, [currentFolder, seedrAllPrefetchedFiles, files, toSeedrStorageFile, fileTypeFilter, fileSearch]);
  // Modals & Drawers
  const [isAddMagnetOpen, setIsAddMagnetOpen] = useState(false);
  const [initialMagnet, setInitialMagnet] = useState('');
  const [prioTorrent, setPrioTorrent] = useState<TorrentItem | null>(null);
  const [isCleanupOpen, setIsCleanupOpen] = useState(false);
  const [isNotificationsOpen, setIsNotificationsOpen] = useState(false);
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedbackType, setFeedbackType] = useState<'review' | 'suggestion' | 'bug'>('review');
  const [feedbackRating, setFeedbackRating] = useState(5);
  const [feedbackMessage, setFeedbackMessage] = useState('');
  const [feedbackName, setFeedbackName] = useState('');
  const [feedbackSubmitting, setFeedbackSubmitting] = useState(false);
  const [feedbackSuccess, setFeedbackSuccess] = useState('');
  const [feedbackError, setFeedbackError] = useState('');
  const [shareFolder, setShareFolder] = useState<StorageFolder | null>(null);
  const [isCreateFolderOpen, setIsCreateFolderOpen] = useState(false);
  const [moveFile, setMoveFile] = useState<StorageFile | null>(null);
  const [renameItem, setRenameItem] = useState<{ id: string; name: string; isFolder: boolean } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<{
    type: 'file' | 'torrent';
    id: string;
    name: string;
    details?: string;
  } | null>(null);

  // Media Player State
  const [activeMediaFile, setActiveMediaFile] = useState<StorageFile | null>(null);
  const [isPlayerMinimized, setIsPlayerMinimized] = useState(false);

  // Previous torrent hashes for completion tracking
  const prevTorrentStates = useRef<Record<string, string>>({});

  // qBittorrent applies stop/start asynchronously. Keep an optimistic transfer
  // state visible for a short reconciliation window so the 1.8s polling loop
  // cannot immediately overwrite a user's pause/resume click with stale state.
  const pendingTransferStates = useRef<Record<string, { state: 'pausedDL' | 'downloading'; expiresAt: number }>>({});

  // Persist navigation so a browser refresh returns to the same page.
  useEffect(() => {
    try {
      window.localStorage.setItem('seedflow_active_tab', activeTab);
    } catch {
      // Storage may be unavailable in restricted browser contexts.
    }
  }, [activeTab]);

  useEffect(() => {
    try {
      if (selectedSeedrFolderId) {
        window.localStorage.setItem('seedflow_seedr_folder', selectedSeedrFolderId);
      } else {
        window.localStorage.removeItem('seedflow_seedr_folder');
      }
    } catch {
      // Storage may be unavailable in restricted browser contexts.
    }
  }, [selectedSeedrFolderId]);

  // Keep the dark/dim theme preference in sync.
  useEffect(() => {
    try {
      window.localStorage.setItem('seedflow_theme', theme);
    } catch {}
    try {
      const root = document.documentElement;
      root.classList.remove('dark', 'dim');
      if (theme === 'dark') {
        root.classList.add('dark');
        root.style.backgroundColor = '#020617';
      } else {
        root.classList.add('dark');
        root.style.backgroundColor = '#0f172a';
      }
    } catch {}
  }, [theme]);

  const seedrDownloadActive = Boolean(
    seedrNotice?.taskId != null && seedrNotice.status !== 'completed'
  );

  const openMetadataSelector = useCallback((source: string) => {
    const value = String(source || '').trim();
    if (!value) return;
    setInitialMagnet(value);
    setInitialSourceUrl('');
    setInitialDescriptorUrl('');
    setBackgroundMetadataJob(null);
    setIsAddMagnetOpen(true);
  }, []);

  const openAddMagnet = useCallback((source = '', sourceUrl = '', descriptorUrl = '') => {
    if (seedrDownloadActive) {
      setSeedrAddBlockedNotice(
        'A Seedr download is already in progress. Free Seedr accounts allow one parallel download. Wait for it to finish before adding another magnet link.'
      );
      setActiveTab('files');
      window.setTimeout(() => setSeedrAddBlockedNotice(null), 5000);
      return;
    }

    setInitialMagnet(source);
    setInitialSourceUrl(sourceUrl);
    setInitialDescriptorUrl(descriptorUrl);
    setIsAddMagnetOpen(true);
  }, [seedrDownloadActive]);

  // Load lightweight application metadata first. The actual files for the
  // currently open folder are fetched separately, after folder metadata exists.
  const loadInitialData = useCallback(async () => {
    try {
      const [uData, sStats, foldData, logs, notifs, cleanup] = await Promise.all([
        api.getUsers(),
        api.getStorageStats(),
        api.getFolders(),
        api.getLogs(),
        api.getNotifications(),
        api.getCleanupSettings()
      ]);

      setUsers(uData.users);
      setActiveUser(uData.activeUser);
      setStorageStats(sStats);
      setFolders(foldData);
      try {
        const rawLocalLogs = window.localStorage.getItem(activityStorageKey);
        const localLogs = rawLocalLogs ? JSON.parse(rawLocalLogs) : [];
        setActivityLogs(Array.isArray(localLogs) && localLogs.length ? localLogs : logs);
      } catch {
        setActivityLogs(logs);
      }
      setNotifications(notifs);
      setCleanupSettings(cleanup);
    } catch (e) {
      console.error('Failed to load initial seedflow metadata:', e);
    }
  }, []);

  useEffect(() => {
    loadInitialData();
  }, [loadInitialData]);

  const loadCurrentFiles = useCallback(async () => {
    try {
      const folderId = currentFolder === '/'
        ? ''
        : String(folders.find(folder => folder.path === currentFolder)?.id || '');
      const fData = await api.getFiles(currentFolder, fileSearch, fileTypeFilter, folderId);
      setFiles(fData);
    } catch (e) {
      console.error('Failed to load current folder files:', e);
      setFiles([]);
    }
  }, [currentFolder, fileSearch, fileTypeFilter, folders]);

  useEffect(() => {
    if (activeTab === 'files') {
      loadCurrentFiles();
    }
  }, [activeTab, loadCurrentFiles]);

  const loadSeedrLibrary = useCallback(async (forceRefresh = false) => {
    if (!seedrConnected) return null;
    setSeedrLoading(true);
    setSeedrError(null);

    try {
      // Check authentication explicitly so the Library panel reports the
      // actual Seedr state instead of turning every failure into a generic
      // library error.
      try {
        const auth = await api.getSeedrAuthStatus();
        if (!auth.configured) {
          setSeedrError('SEEDR_TOKEN_MISSING: Seedr API token is not configured in Render.');
          setSeedrConfigured(false);
          setSeedrLoading(false);
          return null;
        }
        if (!auth.authenticated) {
          try {
            const diagnostic = await api.getSeedrTokenDiagnostic();
            const checks = diagnostic?.checks || {};
            const userCheck = checks.user || {};
            const tokenLabel = userCheck.ok
              ? 'TOKEN_PRESENT_AND_ACCEPTED_BY_SEEDR'
              : 'TOKEN_REJECTED_BY_SEEDR';
            const detail = userCheck.ok
              ? 'Seedr accepted the token through the account endpoint.'
              : 'Seedr rejected the token on the account endpoint.';
            setSeedrError(
              `${tokenLabel}: ${detail} [user=${userCheck.status ?? '?'}]`
            );
          } catch {
            setSeedrError(
              auth.code === 'SEEDR_TOKEN_REJECTED'
                ? 'SEEDR_TOKEN_REJECTED: Seedr rejected the configured API token.'
                : auth.code === 'SEEDR_LIBRARY_ACCESS_DENIED'
                  ? 'SEEDR_LIBRARY_ACCESS_DENIED: Seedr denied the library/auth operation.'
                  : `Seedr authentication check failed: ${auth.message || auth.code}`
            );
          }
          setSeedrConfigured(true);
          setSeedrLoading(false);
          return null;
        }
        setSeedrError(null);
      } catch (error: any) {
        const code = String(error?.code || '').trim();
        setSeedrError(
          code === 'SEEDR_TOKEN_REJECTED'
            ? 'SEEDR_TOKEN_REJECTED: Seedr rejected the configured API token.'
            : code === 'SEEDR_LIBRARY_ACCESS_DENIED'
              ? 'SEEDR_LIBRARY_ACCESS_DENIED: Seedr denied the library/auth operation.'
              : 'SEEDR_QUOTA_UNAVAILABLE: Seedr authentication status is temporarily unavailable.'
        );
        setSeedrLoading(false);
        return null;
      }

      // Keep the Seedr storage/quota cards populated independently of the
      // library metadata request. Quota is small and should never delay the
      // library UI or the download progress bar.
      void api.getSeedrQuota().then(quota => {
        if (!quota.configured) {
          setSeedrQuotaError('SEEDR_TOKEN_MISSING: Seedr API token is not configured in Render.');
          return;
        }        setSeedrQuotaError(null);
        setSeedrQuota({
          maxSpace: quota.maxSpace,          usedSpace: quota.usedSpace,
          remainingSpace: quota.remainingSpace,
        });
      }).catch((error: any) => {
        const code = String(error?.code || '').trim();
        const message =
          code === 'SEEDR_TOKEN_REJECTED'
            ? 'SEEDR_TOKEN_REJECTED: Seedr rejected the configured API token.'
            : code === 'SEEDR_LIBRARY_ACCESS_DENIED'
              ? 'SEEDR_LIBRARY_ACCESS_DENIED: Seedr denied access to account storage information.'
              : 'SEEDR_QUOTA_UNAVAILABLE: Seedr account storage information is unavailable right now.';
        setSeedrQuotaError(message);
      });

      // Refresh the active transfer progress independently of library
      // metadata. This request is intentionally tiny, so the progress bar can
      // move immediately instead of waiting for the library tree.
      const activeTaskId = seedrNotice?.taskId;
      if (activeTaskId && seedrNotice?.status !== 'completed') {
        void api.getSeedrTaskProgress(activeTaskId).then(progressResult => {
          if (progressResult.status === 'not_found') return;
          const progress = Math.max(0, Math.min(100, Number(progressResult.progress) || 0));
          const progressName = String(progressResult.name || '').trim();
          const progressFolderId = String(progressResult.folderId || '').trim();
          setSeedrNotice(prev => {
            if (!prev) return null;

            const canonicalName = String(prev.name || '').trim() || progressName;
            if (
              progressFolderId &&
              canonicalName &&
              canonicalName !== 'Waiting for torrent name…' &&
              canonicalName !== 'Waiting for Seedr metadata…'
            ) {
              rememberSeedrTorrentName(progressFolderId, canonicalName);
            }

            return {
              ...prev,
              name: canonicalName,
              folderId: progressFolderId || prev.folderId || '',
              status: progressResult.status,
              progress,
            };
          });
        }).catch(() => {
          // The normal task poll continues to provide progress updates.
        });
      }

      // Stage 1 — only the metadata required to paint the outer Seedr
      // Library immediately. Do not wait for quota or file rows.
      const result = await api.getSeedrLibrary(forceRefresh);

      setSeedrConfigured(result.configured);
      setSeedrLibraryRoot(result.root);
      setSeedrLibraryFolders(result.folders);
      setSeedrDeletedFolderIds(prev =>
        prev.filter(folderId =>
          result.folders.some(folder => String(folder.folderId || folder.id || '') === folderId)
        )
      );
      setSeedrLoading(false);

      // Stage 2 — immediately load the file rows for every library folder.
      // The old implementation only set seedrPrefetchLoading=true here and
      // waited for a folder click to call getSeedrFolderContents(). That left
      // the root Files view showing a permanent spinner until the user opened
      // a folder and came back. Populate the same cache used by the folder
      // view so the root page is complete on first load.
      const validFolderKeys = new Set(
        result.folders.map(folder => String(folder.folderId || folder.id))
      );
      setSeedrFolderContentsCache(prev =>
        Object.fromEntries(
          Object.entries(prev).filter(([key]) => validFolderKeys.has(key))
        )
      );

      if (result.folders.length === 0) {
        setSeedrPrefetchLoading(false);
      } else {
        setSeedrPrefetchLoading(true);

        void (async () => {
          const foldersToLoad = result.folders.filter(folder =>
            String(folder.folderId || folder.id).trim()
          );

          const loadedEntries: Record<string, Array<{
            id: string;
            streamId?: string;
            name: string;
            size: number;
            folderId: string;
            folderPath: string;
          }>> = {};

          // Keep the initial page responsive when a Seedr account contains
          // many folders, while still loading all folder contents without
          // requiring user navigation.
          const concurrency = 4;
          for (let start = 0; start < foldersToLoad.length; start += concurrency) {
            const batch = foldersToLoad.slice(start, start + concurrency);

            await Promise.all(batch.map(async folder => {
              const folderId = String(folder.folderId || folder.id).trim();
              if (!folderId) return;

              try {
                const existingRequest = seedrFolderContentsRequests.current[folderId];
                const request = existingRequest || (async () => {
                  const contents = await api.getSeedrFolderContents(folderId);
                  const folderPath = folder.path || '/Torrent Studio';
                  return contents.files.map(file => ({
                    id: file.id,
                    streamId: file.streamId,
                    name: file.name,
                    size: Number(file.size) || 0,
                    folderId: file.folderId || folderId,
                    folderPath,
                  }));
                })();

                seedrFolderContentsRequests.current[folderId] = request;
                const mapped = await request;
                loadedEntries[folderId] = mapped;
                setSeedrFolderContentsCache(prev => ({
                  ...prev,
                  [folderId]: mapped,
                }));
              } catch (error) {
                // A single inaccessible/still-indexing folder must not leave
                // the whole Files page in a permanent loading state. The
                // folder can still be retried by clicking it.
                console.warn('Failed to prefetch Seedr folder contents:', folderId, error);
              } finally {
                delete seedrFolderContentsRequests.current[folderId];
              }
            }));
          }

          setSeedrPrefetchLoading(false);
        })();
      }

      // Do not clear the existing file rows during refresh. React keeps the
      // current visible component intact while fresh internal details arrive.
      // The cache effect above swaps them in as soon as they are available.
      return result;
    } catch (error: any) {
      const code = String(error?.code || '').trim();
      const message =
        code === 'SEEDR_TOKEN_REJECTED'
          ? 'SEEDR_TOKEN_REJECTED: Seedr rejected the configured API token.'
          : code === 'SEEDR_LIBRARY_ACCESS_DENIED'
            ? 'SEEDR_LIBRARY_ACCESS_DENIED: Seedr denied access to the Seedr library.'
            : code === 'SEEDR_QUOTA_UNAVAILABLE'
              ? 'SEEDR_QUOTA_UNAVAILABLE: Seedr account storage information is unavailable right now.'
              : error?.message || 'Failed to refresh Seedr metadata';
      setSeedrError(message);
      setSeedrLoading(false);
      setSeedrPrefetchLoading(false);
      return null;
    }
  }, [seedrConnected, rememberSeedrTorrentName]);

  // Keep an opened folder synchronized with the background prefetch cache.
  // Changing the selected folder no longer reruns the entire library request.
  useEffect(() => {
    if (!selectedSeedrFolderId) return;

    const cached = seedrFolderContentsCache[selectedSeedrFolderId];
    if (!cached) return;

    setSeedrFiles(cached);
    setSeedrFolderContentsLoading(false);
  }, [selectedSeedrFolderId, seedrFolderContentsCache]);

  const handleOpenSeedrFolder = useCallback(async (folderId: string) => {
    if (!folderId || folderId === '__root__' || folderId === '__active_seedr__') return;
    const folder = seedrFolderGroups.find(item => item.folderId === folderId);
    const cached = seedrFolderContentsCache[folderId];

    setSelectedSeedrFolderId(folderId);
    setSeedrError(null);

    // Prefetch normally makes this path instant. Fall back to a request only
    // when the user clicks before that background request has completed.
    if (cached) {
      setSeedrFiles(cached);
      setSeedrFolderContentsLoading(false);
      return;
    }

    setSeedrFiles([]);
    setSeedrFolderContentsLoading(true);

    try {
      // If the automatic background prefetch is already running, await that
      // exact request instead of making a second network call.
      const pendingRequest = seedrFolderContentsRequests.current[folderId];
      if (pendingRequest) {
        const mapped = await pendingRequest;
        setSeedrFolderContentsCache(prev => ({ ...prev, [folderId]: mapped }));
        setSeedrFiles(mapped);
        return;
      }

      const result = await api.getSeedrFolderContents(folderId);
      const folderPath = folder?.path || '/Torrent Studio';
      const mapped = result.files.map(file => ({
        id: file.id,
        streamId: file.streamId,
        name: file.name,
        size: Number(file.size) || 0,
        folderId: file.folderId || folderId,
        folderPath,
      }));
      setSeedrFolderContentsCache(prev => ({ ...prev, [folderId]: mapped }));
      setSeedrFiles(mapped);
    } catch (error: any) {
      setSeedrError(error?.message || 'Failed to load Seedr folder contents');
    } finally {
      setSeedrFolderContentsLoading(false);
    }
  }, [seedrFolderGroups, seedrFolderContentsCache, seedrConnected]);

  useEffect(() => {
    if (activeTab === 'files') loadSeedrLibrary();
  }, [activeTab, loadSeedrLibrary]);

  // Load Seedr library metadata once when a personal Seedr connection becomes
  // available so Search can immediately recognize already-prepared torrents.
  useEffect(() => {
    if (seedrConnected) void loadSeedrLibrary();
  }, [seedrConnected]);



  const hasActiveQbtTransfers = useMemo(
    () => torrents.some(t =>
      t.state === 'downloading' ||
      t.state === 'pausedDL' ||
      t.state === 'queuedDL' ||
      t.progress < 1
    ),
    [torrents]
  );

  // Poll qBittorrent compatibility data slowly when idle and faster only while
  // there is an active transfer. Seedr has its own lighter task polling loop.
  useEffect(() => {
    let isMounted = true;

    const pollTorrents = async () => {
      try {
        const torrentList = await api.getTorrents();        if (!isMounted) return;

        // Check for completions to fire push notifications
        torrentList.forEach(t => {          const prevState = prevTorrentStates.current[t.hash];
          if (prevState === 'downloading' && (t.state === 'completed' || t.progress >= 1)) {
            // Transfer finished! Trigger sound and push alert
            playNotificationSound();
            dispatchBrowserNotification(
              `Download Finished: ${t.name}`,
              `Direct streaming and direct download links are now ready in your cloud storage.`
            );
            // Refresh storage & files list
            api.getFiles(currentFolder).then(setFiles).catch(console.error);
            api.getStorageStats().then(setStorageStats).catch(console.error);
            api.getNotifications().then(setNotifications).catch(console.error);
          }
          prevTorrentStates.current[t.hash] = t.state;
        });

        const now = Date.now();
        const reconciledTorrentList = torrentList.map(t => {
          const pending = pendingTransferStates.current[t.hash];
          if (!pending) return t;

          if (pending.expiresAt <= now) {
            delete pendingTransferStates.current[t.hash];
            return t;
          }

          if (pending.state === 'pausedDL') {
            return { ...t, state: 'pausedDL', dlspeed: 0, eta: -1 };
          }

          return {
            ...t,
            state: 'downloading',
            eta: t.eta < 0 ? 0 : t.eta
          };
        });

        setTorrents(reconciledTorrentList);
      } catch (e) {
        console.error('Polling error:', e);
      }
    };

    pollTorrents();
    if (!hasActiveQbtTransfers) {
      return () => {
        isMounted = false;
      };
    }

    const interval = setInterval(pollTorrents, 5000);
    return () => {
      isMounted = false;
      clearInterval(interval);
    };
  }, [currentFolder, hasActiveQbtTransfers]);

  // Actions
  const appendActivityLog = useCallback((log: ActivityLog) => {
    setActivityLogs(prev => {
      const next = [log, ...prev].slice(0, 100);
      try {
        window.localStorage.setItem(activityStorageKey, JSON.stringify(next));
      } catch {}
      return next;
    });
  }, []);

  const handleSearchPrepare = useCallback(async (
    result: {
      title: string;
      size: number;
      infoHash?: string;
      magnetUrl?: string;
      downloadUrl?: string;
      sourceUrl?: string;
      infoUrl?: string;
      indexer?: string;
    },
    metadata?: {
      name: string;
      hash: string;
      files: { index: number; name: string; size: number; path: string; type: string; priority?: number }[];
      totalSize: number;
    }
  ): Promise<{ files: Array<{
    id: string;
    streamId?: string;
    name: string;
    size: number;
    folderId: string;
    folderPath: string;
  }>; deletedFolderIds: string[] }> => {
    const prepareGeneration = ++seedrPrepareGenerationRef.current;
    // Defer replacement until a valid magnet has been resolved and is ready to
    // submit. This prevents a bad/unresolvable selection from cancelling the
    // currently working Seedr task.
    const replacementTaskId = seedrDownloadActive ? seedrNotice?.taskId : null;

    if (!seedrConnected) {
      // Preparing a search result requires a personal Seedr connection.
      // Open the same connection dialog used by the header instead of
      // surfacing a JavaScript error to the user.
      setSeedrConnectError('');
      setSeedrOnboardingOpen(true);
      throw new Error('Connect your Seedr account first.'); 
    }

    if (prepareGeneration !== seedrPrepareGenerationRef.current) {
      throw new Error('Seedr preparation was replaced by a newer selection.');
    }

    let magnet = String(result.magnetUrl || result.downloadUrl || '').trim();
    if (!magnet.toLowerCase().startsWith('magnet:?')) {
      magnet = result.infoHash
        ? 'magnet:?xt=urn:btih:' + result.infoHash.trim()
        : '';
    }

    // Search results now load from listing pages without waiting for every
    // provider detail page. Resolve the magnet only when the user chooses
    // Prepare, keeping slow provider pages out of the initial search request.
    if (!magnet && result.infoUrl) {
      const resolved = await api.resolveSearchMagnet(result.infoUrl, result.indexer || '');
      magnet = String(resolved?.magnet || '').trim();
    }

    if (!magnet.toLowerCase().startsWith('magnet:?')) {
      throw new Error('This search result does not contain a usable magnet link.');
    }

    if (prepareGeneration !== seedrPrepareGenerationRef.current) {
      throw new Error('Seedr preparation was replaced by a newer selection.');
    }

    let resolvedMetadata = metadata;
    // Seedr can resolve metadata itself. When the search provider already
    // supplied a title and byte size, do not block Prepare on a separate
    // libtorrent/DHT metadata lookup that may hang on stale or weak swarms.
    if (!resolvedMetadata && !(Number(result.size) > 0 && String(result.title || '').trim())) {
      resolvedMetadata = await api.inspectMagnet(
        magnet,
        'Downloads',
        result.sourceUrl || '',
        ''
      );
    }

    const torrentName =
      String(resolvedMetadata?.name || '').trim() ||
      String(result.title || '').trim() ||
      'Torrent';
    const requiredBytes = Number(resolvedMetadata?.totalSize || result.size || 0);

    if (prepareGeneration !== seedrPrepareGenerationRef.current) {
      throw new Error('Seedr preparation was replaced by a newer selection.');
    }

    const prepared = await api.prepareSeedrMagnet(
      magnet,
      requiredBytes,
      torrentName,
      replacementTaskId == null ? undefined : replacementTaskId
    );

    // The backend is authoritative for task replacement and also catches a
    // stale task left behind after a page refresh. Reject the older UI waiter
    // so its card stops spinning, without treating that cancellation as an
    // error for the newly selected torrent.
    const cancelledTaskIds = new Set(
      (Array.isArray(prepared?.cancelledTasks) ? prepared.cancelledTasks : [])
        .map((task: any) => String(task?.taskId ?? task?.task_id ?? task?.id ?? '').trim())
        .filter(Boolean)
    );
    for (const cancelledTaskId of cancelledTaskIds) {
      const waiter = seedrPrepareWaiters.current[cancelledTaskId];
      if (!waiter) continue;
      delete seedrPrepareWaiters.current[cancelledTaskId];
      waiter.reject(new Error('Seedr preparation was cancelled by another selection.'));
    }
    if (seedrNotice?.taskId != null && cancelledTaskIds.has(String(seedrNotice.taskId))) {
      setSeedrNotice(null);
    }

    // Prepare can automatically remove older completed Seedr folders to make
    // room for the new torrent. Remove those folders from every local cache
    // immediately so Search cannot keep showing stale Play/Download/Copy
    // buttons for files that no longer exist in Seedr.
    const deletedFolderIds = Array.from(new Set(
      (Array.isArray(prepared?.deletedFolders) ? prepared.deletedFolders : [])
        .map((folder: any) => String(folder?.folder_id ?? folder?.folderId ?? folder?.id ?? '').trim())
        .filter(Boolean)
    ));

    if (deletedFolderIds.length > 0) {
      forgetSeedrFolders(deletedFolderIds);
    }

    const taskId = prepared?.seedrTaskId;
    if (taskId == null || taskId === '') {
      throw new Error('Seedr accepted the request but did not return a task id.');
    }

    const taskKey = String(taskId);
    const waitForCompletion = new Promise<{ files: Array<{
      id: string;
      streamId?: string;
      name: string;
      size: number;
      folderId: string;
      folderPath: string;
    }> }>((resolve, reject) => {
      seedrPrepareWaiters.current[taskKey] = { resolve, reject };
    });

    const initialName = torrentName || String(prepared.seedrFolderName || '').trim() || 'Seedr download';
    setSeedrNotice({
      taskId,
      name: initialName,
      folderName: '',
      folderId: String(prepared.seedrFolderId || '').trim(),
      status: 'waiting',
      progress: 0,
      downloadUrl: null,
      files: [],
      seedrReply: 'Seedr accepted the torrent. Preparing download…',
    });

    const completed = await waitForCompletion;
    return {
      ...completed,
      deletedFolderIds,
    };
  }, [
    seedrConnected,
    seedrDownloadActive,
    seedrNotice,
    rememberSeedrTorrentName,
    forgetSeedrFolders
  ]);

  const handleSearchAdd = async (
    source: string,
    size: number,
    title: string,
    infoHash?: string,
    sourceUrl?: string,
    descriptorUrl?: string,
    metadata?: {
      name: string;
      hash: string;
      files: { index: number; name: string; size: number; path: string; type: string; priority?: number }[];
      totalSize: number;
    }
  ) => {
    const trimmedSource = source.trim();
    const seedrSource =
      source.toLowerCase().startsWith('magnet:?')
        ? source
        : infoHash
          ? `magnet:?xt=urn:btih:${infoHash.trim()}`
          : trimmedSource;

    // Search-result Add is a direct Seedr action. Do not open the manual
    // magnet modal and do not ask the user to paste the magnet again.
    if (!seedrSource) {
      setSeedrAddBlockedNotice('This search result does not contain a usable magnet link.');
      setActiveTab('search');
      window.setTimeout(() => setSeedrAddBlockedNotice(null), 5000);
      return;
    }

    // Search results use metadata before starting Seedr. The search panel
    // may already have this cached from its background prefetch.
    let resolvedMetadata = metadata;
    if (!resolvedMetadata) {
      try {
        resolvedMetadata = await api.inspectMagnet(
          seedrSource,
          'Downloads',
          sourceUrl || '',
          descriptorUrl || ''
        );
      } catch (error: any) {
        setSeedrAddBlockedNotice(error?.message || 'Could not resolve torrent metadata.');
        setActiveTab('search');
        window.setTimeout(() => setSeedrAddBlockedNotice(null), 5000);
        return;
      }
    }

    // Seedr receives the original search magnet unchanged. Metadata is used
    // only to make the transfer name available immediately.
    const torrentName =
      String(resolvedMetadata?.name || '').trim() ||
      String(title || '').trim() ||
      'Torrent';

    await handleAddMagnet(
      seedrSource,
      'video',
      undefined,
      undefined,
      infoHash || resolvedMetadata?.hash || undefined,
      'seedr',
      undefined,
      undefined,
      torrentName,
      Number(resolvedMetadata?.totalSize || size || 0)
    );
  };

  const handleAddMagnet = async (
    magnet: string,
    category: string,
    selectedFiles?: number[],
    manifest?: { index?: number; name: string; size: number; priority: number }[],
    existingHash?: string,
    forceBackend?: 'seedr' | 'qbittorrent',
    selectedNames?: string[],
    seedrTaskId?: number | string,
    torrentName?: string,
    requiredBytes?: number
  ) => {
    try {
      if (seedrDownloadActive && forceBackend !== 'qbittorrent') {
        const error = new Error(
          'A Seedr download is already in progress. Free Seedr accounts allow one parallel download. Wait for it to finish before adding another magnet link.'
        );
        (error as any).code = 'SEEDR_PARALLEL_DOWNLOAD_LIMIT';
        throw error;
      }

      // Pre-check Seedr quota before creating the task. Seedr can reject an
      // over-quota torrent with a provider-specific 413 reason, so relying
      // only on the provider response made the warning inconsistent.
      if (forceBackend !== 'qbittorrent') {
        let required = Number(requiredBytes || 0);
        if (!required && Array.isArray(manifest)) {
          required = manifest
            .filter(file => Number(file.priority || 0) > 0)
            .reduce((sum, file) => sum + Math.max(0, Number(file.size || 0)), 0);
        }

        if (required > 0) {
          try {
            const quota = await api.getSeedrQuota();
            setSeedrQuota({
              maxSpace: quota.maxSpace,
              usedSpace: quota.usedSpace,
              remainingSpace: quota.remainingSpace,
            });

            if (quota.remainingSpace < required) {
              setActiveTab('search');
              setSeedrInsufficientSpacePrompt({
                requiredBytes: required,
                remainingBytes: quota.remainingSpace,
                magnet,
                category,
                selectedFiles,
                manifest,
                existingHash,
              });
              return;
            }
          } catch {
            // If quota lookup is temporarily unavailable, let Seedr make the
            // authoritative decision below rather than blocking the add.
          }
        }
      }

      const result = await api.addMagnet(
        magnet,
        category,
        selectedFiles,
        manifest,
        existingHash,
        forceBackend,
        selectedNames,
        seedrTaskId,
        torrentName
      );
      if (result.backend === 'seedr') {
        const initialTorrentName = String(torrentName || '').trim();
        const returnedFolderId = String((result as any).seedrFolderId || (result as any).seedrResponse?.folder_id || '').trim();
        if (returnedFolderId && initialTorrentName) {
          rememberSeedrTorrentName(returnedFolderId, initialTorrentName);
        }

        setSeedrNotice({
          taskId: result.seedrTaskId ?? null,
          name: String(torrentName || '').trim() || (() => {
            const response: any = result.seedrResponse;
            const responseName = String(
              response?.name ??
              response?.task?.name ??
              response?.title ??
              ''
            ).trim();
            if (responseName) return responseName;

            const selectedManifest = (manifest || []).filter(file => Number(file.priority || 0) > 0);
            if (selectedManifest.length === 1) return selectedManifest[0].name;
            if (selectedManifest.length > 1) {
              return selectedManifest[0].name + ` + ${selectedManifest.length - 1} more`;
            }

            return 'Waiting for torrent name…';
          })(),
          folderName: '',
          status: 'waiting',
          progress: 0,
          downloadUrl: null,
          // Only the files selected in the manifest are represented in the
          // pending UI. The backend applies the Seedr unwanted-file bitmap
          // immediately after creating the task; no pause/resume is used.
          files: (manifest || [])            .map((file, index) => ({ file, index }))
            .filter(({ file }) => Number(file.priority || 0) > 0)
            .map(({ file, index }) => ({
              id: `pending-${result.seedrTaskId ?? 'task'}-${index}`,
              name: file.name,              size: Number(file.size || 0),
              folderId: '__pending__',
              folderPath: '/Currently Downloading',
              url: null,
              available: false,
            })),
          seedrReply: (() => {
            const response: any = result.seedrResponse;
            const state = response?.state ?? response?.task?.state ?? response?.status ?? response?.task?.status;
            return state ? `Seedr replied: ${String(state)}` : 'Seedr replied: task accepted';
          })(),
          selectionApplied: Boolean((result as any).selectionApplied),
          selectionError: (result as any).selectionError || null,
        });;
      } else {
        setSeedrNotice(null);
      }
      if (result.backend === 'seedr') {
        const loggedName =
          String(torrentName || '').trim() ||
          String(result.seedrResponse?.name || result.seedrResponse?.task?.name || '').trim() ||
          'Torrent';

        const taskId = result.seedrTaskId ?? result.seedrResponse?.task_id ?? result.seedrResponse?.id ?? '';
        const folderId = result.seedrFolderId ?? result.seedrResponse?.folder_id ?? result.seedrResponse?.task?.folder_id ?? '';

        appendActivityLog({
          id: `seedr-add-${taskId || Date.now()}-${Date.now()}`,
          timestamp: Date.now(),
          type: 'torrent',
          userName: activeUser?.name || 'Seedr User',
          userId: activeUser?.id || 'seedr-user',
          action: 'Torrent added to Seedr',
          details: `${loggedName}${taskId ? ` • Task ${taskId}` : ''}${folderId ? ` • Folder ${folderId}` : ''} • Auto-delete in 2 hours`,
          status: 'success'
        });
      }

      // The add response is the important operation. Refresh secondary UI
      // state in the background so the Add button does not stay blocked on
      // extra qBittorrent/Seedr requests.
      void api.getTorrents()
        .then(setTorrents)
        .catch((refreshError) =>
          console.warn('Torrent added, but transfers could not be refreshed yet:', refreshError)
        );

      void api.getStorageStats()
        .then(setStorageStats)
        .catch((refreshError) =>
          console.warn('Torrent added, but storage stats could not be refreshed yet:', refreshError)
        );

      setActiveTab(result.backend === 'seedr' ? 'files' : 'transfers');
    } catch (error: any) {
      if (error?.code === 'SEEDR_INSUFFICIENT_SPACE') {
        let required = Number(error.requiredBytes || requiredBytes || 0);
        if (!required && Array.isArray(manifest)) {
          required = manifest
            .filter(file => Number(file.priority || 0) > 0)
            .reduce((sum, file) => sum + Math.max(0, Number(file.size || 0)), 0);
        }

        let remaining = Number(error.remainingSpace || 0);
        if (!remaining) {
          try {
            const quota = await api.getSeedrQuota();
            remaining = Number(quota.remainingSpace || 0);
            setSeedrQuota({
              maxSpace: Number(quota.maxSpace || 0),
              usedSpace: Number(quota.usedSpace || 0),
              remainingSpace: remaining,
            });
          } catch {
            // Keep the provider error visible even if quota refresh fails.
          }
        }

        setActiveTab('search');
        setSeedrInsufficientSpacePrompt({
          requiredBytes: required,
          remainingBytes: remaining,
          magnet,
          category,
          selectedFiles,
          manifest,
          existingHash,
        });
        return;
      }
      throw error;
    }
  };

  useEffect(() => {
    try {
      if (seedrNotice?.taskId != null && seedrNotice.status !== 'completed') {
        window.localStorage.setItem(seedrNoticeStorageKey, JSON.stringify(seedrNotice));
      } else {
        window.localStorage.removeItem(seedrNoticeStorageKey);
      }
    } catch {
      // Local storage may be unavailable in restricted browser contexts.
    }
  }, [seedrNotice]);

  // Completed Seedr notices are only a short-lived confirmation. Keep the
  // transfer screen clean by removing the card automatically after 3 seconds.
  useEffect(() => {
    if (!seedrNotice?.taskId || seedrNotice.status !== 'completed') return;

    const timeoutId = window.setTimeout(() => {
      setSeedrNotice(null);
    }, 8000);

    return () => window.clearTimeout(timeoutId);
  }, [seedrNotice?.taskId, seedrNotice?.status]);

  useEffect(() => {
    if (!seedrNotice?.taskId || seedrNotice.status === 'completed') return;

    let active = true;
    let timeoutId: number | null = null;

    const scheduleNextPoll = (delayMs: number) => {
      if (!active) return;
      timeoutId = window.setTimeout(poll, delayMs);
    };

    const applyProgress = (result: {
      status: 'waiting' | 'downloading' | 'completed' | 'not_found';
      progress: number;
      name?: string;
      folderId?: string;
    }) => {
      const progress = Math.max(0, Math.min(100, Number(result.progress) || 0));
      const completed = result.status === 'completed';

      setSeedrNotice(prev => {
        if (!prev) return null;

        const canonicalName = String(prev.name || '').trim() || String(result.name || '').trim();
        const resolvedFolderId = String(result.folderId || prev.folderId || '').trim();

        if (
          resolvedFolderId &&
          canonicalName &&
          canonicalName !== 'Waiting for torrent name…' &&
          canonicalName !== 'Waiting for Seedr metadata…'
        ) {
          rememberSeedrTorrentName(resolvedFolderId, canonicalName);
        }

        return {
          ...prev,
          name: canonicalName,
          folderId: resolvedFolderId,
          // Keep the polling effect alive long enough to fetch the completed
          // task details and promote the folder into the library. Marking the
          // notice "completed" here causes React to tear down this effect
          // immediately, which can abort the rest of the completion flow.
          status: completed ? 'downloading' : result.status,
          progress: completed ? 100 : progress,
        };
      });

      return completed;
    };

    const poll = async () => {
      if (!active) return;

      try {
        // This endpoint only asks Seedr for task state/progress, so it is much
        // faster than loading task contents, folder names, and file URLs.
        const progressResult = await api.getSeedrTaskProgress(seedrNotice.taskId!);
        if (!active) return;

        if (progressResult.status === 'not_found') {
          const waiterKey = String(seedrNotice.taskId);
          const waiter = seedrPrepareWaiters.current[waiterKey];
          if (waiter) {
            delete seedrPrepareWaiters.current[waiterKey];
            waiter.reject(new Error('Seedr could not find the loading task.'));
          }
          setSeedrNotice(null);
          setSeedrAddBlockedNotice(null);
          try {
            window.localStorage.removeItem(seedrNoticeStorageKey);
          } catch {}
          return;
        }

        const completed = applyProgress(progressResult);

        if (!completed) {
          // Poll frequently so the visible progress bar moves as soon as Seedr
          // reports a newer value.
          scheduleNextPoll(progressResult.status === 'waiting' ? 10000 : 4000);
          return;
        }

        // Only when complete do we make the heavier request for the final
        // files/download URLs before promoting the task into the library.
        const result = await api.getSeedrTask(seedrNotice.taskId!);
        if (!active) return;

        setSeedrNotice(prev => {
          if (!prev) return null;

          const resolvedFolderId = String(
            (result as any).folderId ||
            progressResult.folderId ||
            prev.folderId ||
            ''
          ).trim();
          const canonicalName = String(
            prev.name ||
            (result as any).name ||
            progressResult.name ||
            ''
          ).trim();

          if (
            resolvedFolderId &&
            canonicalName &&
            canonicalName !== 'Waiting for torrent name…' &&
            canonicalName !== 'Waiting for Seedr metadata…'
          ) {
            rememberSeedrTorrentName(resolvedFolderId, canonicalName);
          }

          return {
            ...prev,
            name: canonicalName,
            folderName: '',
            folderId: resolvedFolderId,
            // Keep the active library card mounted until the completed folder
            // has been inserted into seedrLibraryFolders below. Otherwise the
            // card can disappear for a render while Seedr's library catches up.
            status: 'downloading',
            progress: 100,
            downloadUrl: result.downloadUrl,
            files: Array.isArray(result.files) && result.files.length > 0 ? result.files : prev.files,
          };
        });

        const completedFolderId = String(
          (result as any).folderId ||
          (Array.isArray((result as any).files)
            ? (result as any).files.find((file: any) => String(file?.folderId || '').trim())?.folderId            : '') ||
          progressResult.folderId ||
          seedrNotice.folderId ||
          ''
        ).trim();
        const completedTorrentName = String(
          seedrNotice.name ||
          (result as any).name ||
          progressResult.name ||
          ''
        ).trim();

        const resolvePrepareWaiter = (files: Array<{
          id: string;
          streamId?: string;
          name: string;
          size: number;
          folderId: string;
          folderPath: string;
        }>) => {
          const waiterKey = String(seedrNotice.taskId);
          const waiter = seedrPrepareWaiters.current[waiterKey];
          if (!waiter) return;
          delete seedrPrepareWaiters.current[waiterKey];
          waiter.resolve({ files });
        };

        const refreshCompletedLibrary = async () => {
          // Seedr can report a task as complete before the new folder appears
          // in the library tree. Add the completed folder to the UI immediately
          // from the completed task/folder response, then keep reconciling with
          // the real Seedr library until it becomes visible there too.
          let eagerFiles: Array<{
            id: string;
            streamId?: string;
            name: string;
            size: number;
            folderId: string;
            folderPath: string;
          }> = [];

          let eagerFolderId = completedFolderId;

          if (completedFolderId) {
            try {
              const contents = await api.getSeedrFolderContents(completedFolderId);
              eagerFiles = contents.files.map(file => ({
                id: file.id,
                streamId: file.streamId,
                name: file.name,
                size: Number(file.size) || 0,
                folderId: file.folderId || completedFolderId,
                folderPath: '/Torrent Studio/' + (completedTorrentName || 'Downloads'),
              }));

              if (eagerFiles.length > 0) {
                setSeedrFolderContentsCache(prev => ({
                  ...prev,
                  [completedFolderId]: eagerFiles,
                }));
                if (selectedSeedrFolderId === completedFolderId) {
                  setSeedrFiles(eagerFiles);
                  setSeedrFolderContentsLoading(false);
                }
              }
            } catch (error) {
              console.warn('Completed Seedr folder is not visible yet:', error);
            }

            // Show the completed folder immediately even when the Seedr library
            // index is still lagging behind the task completion.
            const eagerSize = eagerFiles.reduce((sum, file) => sum + Number(file.size || 0), 0);
            const eagerName = completedTorrentName || 'Completed Seedr download';
            setSeedrLibraryFolders(prev => {
              const exists = prev.some(folder => String(folder.folderId || folder.id) === completedFolderId);
              if (exists) return prev;

              return [
                {
                  id: completedFolderId,
                  folderId: completedFolderId,
                  name: eagerName,
                  path: '/Torrent Studio/' + eagerName,
                  filesCount: eagerFiles.length || 1,
                  totalSize: eagerSize || Number((result as any)?.size || 0),
                  folderCount: 0,
                  active: false,
                  progress: 100,
                },
                ...prev,
              ];
            });

            // The completed folder is now present in the UI state. Only now
            // mark the transfer notice completed, so React cannot remove the
            // active card before the new folder has been mounted.
            setSeedrNotice(prev => (
              prev ? { ...prev, status: 'completed', progress: 100 } : null
            ));

            if (eagerFiles.length > 0) {
              resolvePrepareWaiter(eagerFiles);
            }
          }

          for (let attempt = 0; attempt < 12; attempt += 1) {
            const refreshed = await loadSeedrLibrary(true);

            // loadSeedrLibrary replaces the folder array with the latest API
            // response. Preserve our eager completed folder if Seedr's index is
            // still catching up.
            if (eagerFolderId) {
              const folderIdForMerge = eagerFolderId;
              const mergeName = completedTorrentName || 'Completed Seedr download';
              setSeedrLibraryFolders(prev => {
                const exists = prev.some(folder => String(folder.folderId || folder.id) === folderIdForMerge);
                if (exists) return prev;

                const mergeSize = eagerFiles.reduce((sum, file) => sum + Number(file.size || 0), 0);
                return [
                  {
                    id: folderIdForMerge,
                    folderId: folderIdForMerge,
                    name: mergeName,
                    path: '/Torrent Studio/' + mergeName,
                    filesCount: eagerFiles.length || 1,
                    totalSize: mergeSize,
                    folderCount: 0,
                    active: false,
                    progress: 100,
                  },
                  ...prev,
                ];
              });
            }

            const completedFolder = refreshed?.folders?.find(folder => {
              const folderId = String(folder.folderId || folder.id || '').trim();
              const folderName = String(folder.name || '').trim();
              return (
                (completedFolderId && folderId === completedFolderId) ||
                (!completedFolderId &&
                  completedTorrentName &&
                  folderName.toLowerCase() === completedTorrentName.toLowerCase())
              );
            });

            if (completedFolder) {
              const resolvedFolderId = String(
                completedFolder.folderId || completedFolder.id || completedFolderId || ''
              ).trim();

              // Once the real library index catches up, refresh its file rows too.
              if (resolvedFolderId) {
                try {
                  const contents = await api.getSeedrFolderContents(resolvedFolderId);
                  const mapped = contents.files.map(file => ({
                    id: file.id,
                    streamId: file.streamId,
                    name: file.name,
                    size: Number(file.size) || 0,
                    folderId: file.folderId || resolvedFolderId,
                    folderPath: completedFolder.path,
                  }));

                  setSeedrFolderContentsCache(prev => ({
                    ...prev,
                    [resolvedFolderId]: mapped,
                  }));

                  const waiterKey = String(seedrNotice.taskId);
                  const waiter = seedrPrepareWaiters.current[waiterKey];
                  if (waiter && mapped.length > 0) {
                    delete seedrPrepareWaiters.current[waiterKey];
                    waiter.resolve({ files: mapped });
                  }

                  if (selectedSeedrFolderId === resolvedFolderId) {
                    setSeedrFiles(mapped);
                    setSeedrFolderContentsLoading(false);
                  }
                } catch (error) {
                  console.warn('Failed to load completed Seedr file details:', error);
                }
              }

              break;
            }

            if (attempt < 11) {
              await new Promise(resolve => window.setTimeout(resolve, 1500));
            }
          }
        };

        void refreshCompletedLibrary();
        setActiveSeedrFolderOpen(false);

        const immediateFiles = Array.isArray((result as any).files)
          ? (result as any).files
              .filter((file: any) => String(file?.id || file?.streamId || '').trim())
              .map((file: any) => ({
                id: String(file.id || file.streamId),
                streamId: file.streamId ? String(file.streamId) : undefined,
                name: String(file.name || 'Seedr file'),
                size: Number(file.size || 0),
                folderId: String(file.folderId || completedFolderId || ''),
                folderPath: String(file.folderPath || '/Torrent Studio/' + (completedTorrentName || 'Downloads')),
              }))
          : [];

        if (immediateFiles.length > 0) {
          resolvePrepareWaiter(immediateFiles);
        }

        playNotificationSound();
        dispatchBrowserNotification(
          `Ready: ${completedTorrentName || 'Seedr download'}`,
          'Your Seedr file is ready to play, download, or copy its link.'
        );

      } catch {
        scheduleNextPoll(2500);
      }
    };

    void poll();

    return () => {
      active = false;
      if (timeoutId !== null) window.clearTimeout(timeoutId);
    };
  }, [seedrNotice?.taskId, seedrNotice?.status, loadSeedrLibrary, rememberSeedrTorrentName]);

  const handleCancelSeedrDownload = async () => {
    const taskId = seedrNotice?.taskId;
    if (taskId == null || isCancellingSeedr) return;

    const currentNotice = seedrNotice;
    try {
      setIsCancellingSeedr(true);
      const cancelled = await api.deleteSeedrTask(taskId);
      if (cancelled?.folderDeleted && cancelled?.folderId) {
        forgetSeedrFolders([String(cancelled.folderId)]);
      }
      const waiterKey = String(taskId);
      const waiter = seedrPrepareWaiters.current[waiterKey];
      if (waiter) {
        delete seedrPrepareWaiters.current[waiterKey];
        waiter.reject(new Error('Seedr preparation was cancelled.'));
      }
      setSeedrNotice(null);
    } catch (error: any) {
      console.error('Failed to cancel Seedr download:', error);
      setSeedrNotice(currentNotice);
      setSeedrAddBlockedNotice(
        error?.message || 'Failed to cancel the Seedr download. Please try again.'
      );
      window.setTimeout(() => setSeedrAddBlockedNotice(null), 5000);
    } finally {
      setIsCancellingSeedr(false);
    }
  };

  const handleStreamTorrent = (torrent: TorrentItem) => {
    const streamableFile = torrent.files?.find(file => {
      if (file.priority <= 0 || file.progress < 0.999) return false;
      return /\.(mkv|mp4|m4v|webm|mov|avi|mp3|wav|flac|aac|ogg|m4a)$/i.test(file.name);
    });

    if (!streamableFile) return;

    const lower = streamableFile.name.toLowerCase();
    const type: StorageFile['type'] =
      /\.(mkv|mp4|m4v|webm|mov|avi)$/i.test(lower) ? 'video' : 'audio';

    const syntheticFile: StorageFile = {
      id: `torrent-${torrent.hash}-${streamableFile.index}`,
      name: streamableFile.name.split('/').pop() || streamableFile.name,
      path: streamableFile.path || streamableFile.name,
      folder: torrent.category || '/',
      size: streamableFile.size,
      type,
      mimeType: type === 'video' ? 'video/mp4' : 'audio/mpeg',
      createdAt: torrent.completion_on ? torrent.completion_on * 1000 : Date.now(),
      torrentHash: torrent.hash,
      isStreamable: true,
      ownerId: activeUser?.id || 'user_admin',
      ownerName: activeUser?.name || 'Admin',
      downloadUrl: `/api/torrents/download/${encodeURIComponent(torrent.hash)}/${streamableFile.index}`,
      streamUrl: `/api/torrents/stream/${encodeURIComponent(torrent.hash)}/${streamableFile.index}`
    };

    setActiveMediaFile(syntheticFile);
    setIsPlayerMinimized(false);
  };

  const handlePauseTorrent = async (hash: string) => {
    // Update immediately and hold that state through the next few polling
    // cycles while qBittorrent finishes applying stop().
    const previous = torrents;
    pendingTransferStates.current[hash] = {
      state: 'pausedDL',
      expiresAt: Date.now() + 5000
    };

    setTorrents(prev =>
      prev.map(t =>        t.hash === hash
          ? { ...t, state: 'pausedDL', dlspeed: 0, eta: -1 }
          : t
      )
    );

    try {      await api.pauseTorrent(hash);
    } catch (error) {
      delete pendingTransferStates.current[hash];
      console.error('Failed to pause torrent:', error);
      setTorrents(previous);
      throw error;
    }
  };

  const handleResumeTorrent = async (hash: string) => {
    const previous = torrents;
    pendingTransferStates.current[hash] = {
      state: 'downloading',
      expiresAt: Date.now() + 5000
    };

    setTorrents(prev =>
      prev.map(t =>
        t.hash === hash
          ? { ...t, state: 'downloading', eta: t.eta < 0 ? 0 : t.eta }
          : t
      )
    );

    try {
      await api.resumeTorrent(hash);
    } catch (error) {
      delete pendingTransferStates.current[hash];
      console.error('Failed to resume torrent:', error);
      setTorrents(previous);
      throw error;
    }
  };

  const handleDeleteTorrent = (hash: string) => {
    const torrent = torrents.find(t => t.hash === hash);
    if (!torrent) return;
    setDeleteTarget({
      type: 'torrent',
      id: torrent.hash,
      name: torrent.name,
      details: `${formatBytes(torrent.total_size)} • Progress: ${Math.round(torrent.progress * 100)}%`
    });
  };

  const handleUpdateFilePriority = async (hash: string, fileId: string, priority: number) => {
    const ids = fileId.split('|').map(Number).filter(Number.isFinite);
    const previous = torrents;

    // Reflect checkbox/priority changes immediately in the main torrent card.
    setTorrents(prev =>
      prev.map(t => {
        if (t.hash !== hash) return t;

        const nextFiles = (t.files || []).map(file =>
          ids.includes(file.index)
            ? {
                ...file,
                priority,
                progress: priority === 0 ? 0 : file.progress
              }
            : file
        );

        const selectedSize = nextFiles
          .filter(file => file.priority > 0)
          .reduce((sum, file) => sum + file.size, 0);

        return {
          ...t,
          files: nextFiles,
          selected_size: selectedSize
        };
      })
    );

    try {
      await api.setFilePriority(hash, fileId, priority);
    } catch (error) {
      console.error('Failed to update file priority:', error);
      setTorrents(previous);
      throw error;
    }

    api.getTorrents()
      .then(setTorrents)
      .catch(error => console.error('Failed to refresh torrents after priority update:', error));
  };


  const handleDownloadSeedrFolder = async (folderId: string, folderName = '') => {
    try {
      // Open the navigation synchronously from the click so the browser never
      // treats it as an async popup. The backend supplies Content-Disposition
      // with the requested filename.
      const zipName = folderName && !folderName.toLowerCase().endsWith('.zip')
        ? folderName + '.zip'
        : folderName;
      api.openSeedrFolderDownload(folderId, zipName);
    } catch (error) {
      console.error('Failed to start Seedr folder download:', error);
      setSeedrError(error instanceof Error ? error.message : 'Failed to start Seedr folder download');
    }
  };

  const handleCopySeedrFileUrl = async (fileId: string) => {
    try {
      const result = await api.getSeedrFileDownload(fileId);
      await navigator.clipboard.writeText(result.url);
      setCopiedSeedrFileId(fileId);
      window.setTimeout(() => {
        setCopiedSeedrFileId(current => current === fileId ? null : current);
      }, 1800);
    } catch (error) {
      console.error('Failed to copy Seedr download link:', error);
      setSeedrError(error instanceof Error ? error.message : 'Failed to copy Seedr download link');
    }
  };

  const handleDownloadSeedrFile = async (fileId: string, fileName = '') => {
    const popup = window.open('', '_blank', 'noopener,noreferrer');
    try {
      // Resolve the temporary Seedr URL only because the user clicked Download.
      // The final transfer then goes directly from Seedr to the browser.
      const result = await api.getSeedrFileDownload(fileId);
      const target = result.url;
      if (popup) {
        popup.location.href = target;
      } else {
        window.location.href = target;
      }
    } catch (error) {
      if (popup) popup.close();
      console.error('Failed to start Seedr download:', error);
      setSeedrError(error instanceof Error ? error.message : 'Failed to start Seedr download');
    }
  };


  const findSeedrSubtitleTracks = useCallback((
    file: { id: string; name: string; folderId: string; folderPath: string },
    apiOrigin: string,
    siblingFiles?: Array<{ id: string; name: string; folderId?: string }>
  ): StorageFile['subtitleTracks'] => {
    const extension = (name: string) => name.match(/\.([^.]+)$/)?.[1]?.toLowerCase() || '';
    const videoExt = extension(file.name);
    if (!/^(mkv|mp4|m4v|webm|mov|avi|ts)$/.test(videoExt)) return [];

    const videoBase = file.name.slice(0, -(videoExt.length + 1)).trim().toLowerCase();
    const cachedSiblings = siblingFiles?.length
      ? siblingFiles
      : file.folderId
        ? (seedrFolderContentsCache[file.folderId] || [])
        : seedrAllPrefetchedFiles.filter(item =>
            item.folderPath === file.folderPath || item.folderId === file.folderId
          );

    const languageNames: Record<string, string> = {
      en: 'English', eng: 'English', hi: 'Hindi', hin: 'Hindi',
      ar: 'Arabic', ara: 'Arabic', bn: 'Bengali', ben: 'Bengali',
      es: 'Spanish', spa: 'Spanish', fr: 'French', fra: 'French',
      de: 'German', deu: 'German', it: 'Italian', ita: 'Italian',
      pt: 'Portuguese', por: 'Portuguese', ru: 'Russian', rus: 'Russian',
      ja: 'Japanese', jpn: 'Japanese', ko: 'Korean', kor: 'Korean',
      zh: 'Chinese', zho: 'Chinese'
    };

    const subtitleFiles = cachedSiblings
      .filter(item => item.id !== file.id && /\.(srt|vtt)$/i.test(item.name));
    const videoFiles = cachedSiblings
      .filter(item => /\.(mkv|mp4|m4v|webm|mov|avi|ts)$/i.test(item.name));
    const hasMultipleVideos = videoFiles.length > 1;

    return subtitleFiles
      .filter(item => {
        const subtitleExt = extension(item.name);
        const subtitleBase = item.name.slice(0, -(subtitleExt.length + 1)).trim().toLowerCase();
        const exactMatch = subtitleBase === videoBase ||
          subtitleBase.startsWith(videoBase + '.') ||
          subtitleBase.startsWith(videoBase + ' ');
        // If this folder contains only one video, any SRT/VTT beside it is a
        // valid sidecar subtitle even when the subtitle has a generic name
        // such as "English.srt" or "Subs.srt". With multiple videos, require
        // a filename match so subtitles cannot be attached to the wrong video.
        return exactMatch || !hasMultipleVideos;
      })
      .map((item, index) => {
        const subtitleExt = extension(item.name);
        const subtitleBase = item.name.slice(0, -(subtitleExt.length + 1)).trim();
        const suffix = subtitleBase.slice(videoBase.length).replace(/^[. _-]+/, '').trim();
        const languageKey = suffix.split(/[. _-]+/)[0]?.toLowerCase() || '';
        const language = languageNames[languageKey] ? languageKey : 'en';
        return {
          index,
          language,
          title: languageNames[languageKey] || suffix || subtitleBase || 'Subtitles',
          codec: subtitleExt.toUpperCase(),
          url: apiOrigin + '/api/seedr/files/' + encodeURIComponent(item.id) +
            '/subtitle?filename=' + encodeURIComponent(item.name)
        };
      });
  }, [seedrAllPrefetchedFiles, seedrFolderContentsCache]);

  const handleStreamSeedrFile = async (file: { id: string; streamId?: string; name: string; size: number; folderId: string; folderPath: string }) => {
    // Restore the tested Seedr direct-browser-stream flow. The backend chooses
    // the correct presentation: a Range-aware direct stream when Seedr gives
    // a browser-playable presentation, otherwise the HLS proxy.
    setSeedrStreamLoadingId(file.id);
    try {
      const type: StorageFile['type'] =
        /\.(mkv|mp4|m4v|webm|mov|avi|m3u8|ts)$/i.test(file.name) ? 'video' :
        /\.(mp3|wav|flac|aac|ogg|m4a)$/i.test(file.name) ? 'audio' :
        'document';

      if (type !== 'video' && type !== 'audio') {
        setSeedrError('This Seedr file is not a supported video or audio file.');
        return;
      }

      setSeedrError(null);
      const result = await api.getSeedrFileStream(file.streamId || file.id, file.name, type);
      const apiOrigin = (() => {
        try {
          return new URL(result.url, window.location.origin).origin;
        } catch {
          return window.location.origin;
        }
      })();

      let subtitleSiblings: Array<{ id: string; name: string; folderId?: string }> = [];
      if (type === 'video' && file.folderId) {
        try {
          // A torrent folder can contain the video and a separate .srt/.vtt.
          // Use the cache first, but do not trust a cache entry that contains
          // no sidecar subtitles: the folder may have been indexed before the
          // subtitle was added. In that case refresh the tiny Seedr folder
          // listing once so the player can discover the new sidecar.
          const cached = seedrFolderContentsCache[file.folderId];
          if (cached?.length) {
            subtitleSiblings = cached;

            const cachedHasSidecar = cached.some(item => /\.(srt|vtt)$/i.test(item.name));
            if (!cachedHasSidecar) {
              const contents = await api.getSeedrFolderContents(file.folderId);
              subtitleSiblings = contents.files || [];
              setSeedrFolderContentsCache(prev => ({
                ...prev,
                [file.folderId]: contents.files || [],
              }));
            }
          } else {
            const contents = await api.getSeedrFolderContents(file.folderId);
            subtitleSiblings = contents.files || [];
            setSeedrFolderContentsCache(prev => ({
              ...prev,
              [file.folderId]: contents.files || [],
            }));
          }
        } catch (subtitleFolderError) {
          console.debug('Seedr sidecar subtitle discovery skipped:', subtitleFolderError);
        }
      }

      const subtitleTracks = type === 'video'
        ? findSeedrSubtitleTracks(file, apiOrigin, subtitleSiblings)
        : [];
      const audioTracks: StorageFile['audioTracks'] = [];

      const syntheticFile: StorageFile = {
        id: 'seedr-' + file.id,
        name: result.name || file.name,
        path: file.folderPath === '/' ? '/' + file.name : file.folderPath + '/' + file.name,
        folder: file.folderPath,
        size: file.size,
        type,
        mimeType: type === 'video' ? 'video/mp4' : 'audio/mpeg',
        createdAt: Date.now(),
        ownerId: activeUser?.id || 'user_admin',
        ownerName: activeUser?.name || 'Admin',
        isStreamable: true,
        // IMPORTANT: use the backend browser URL, not the external Seedr URL.
        // For direct presentations this is /api/seedr/media/video/{id}; for
        // HLS presentations it is /api/seedr/hls/{id}.
        streamUrl: result.url,
        externalStreamUrl: result.externalUrl,
        streamId: result.resolvedFileId || file.streamId || file.id,
        audioTracks,
        subtitleTracks,
        downloadUrl: API_BASE + '/api/seedr/files/' + encodeURIComponent(file.id) + '/download',
      };

      setActiveMediaFile(syntheticFile);
      setIsPlayerMinimized(false);
    } catch (error) {
      console.error('Failed to create Seedr stream URL:', error);
      setSeedrError(error instanceof Error ? error.message : 'Failed to create Seedr stream URL');
    } finally {
      setSeedrStreamLoadingId(current => current === file.id ? null : current);
    }
  };
  const handleDeleteSeedrFile = async (file: { id: string; name: string; size: number; folderId: string; folderPath: string }) => {    setSeedrDeleteNotice('Deleting…');
    try {
      // A single-file Seedr folder is represented directly in My Cloud Files.
      // In that special case, delete the whole Seedr folder rather than only
      // the file. Files inside multi-file folders still use file deletion.
      const group = seedrFolderGroups.find(item => item.folderId === file.folderId);
      const isSingleFileFolder =
        file.folderId !== '__root__' &&
        group?.filesCount === 1;

      if (isSingleFileFolder) {
        await api.deleteSeedrFolder(file.folderId);
        setSeedrFiles(prev => prev.filter(item => item.folderId !== file.folderId));
        // The Files tab also renders the lazily-prefetched Seedr files cache.
        // Remove the deleted folder from that cache as well, otherwise the
        // deleted file remains visible at the root after returning from the
        // Seedr folder view.
        setSeedrFolderContentsCache(prev => {
          if (!(file.folderId in prev)) return prev;
          const next = { ...prev };
          delete next[file.folderId];
          return next;
        });
      } else {
        await api.deleteSeedrFile(file.id);
        setSeedrFiles(prev => prev.filter(item => item.id !== file.id));
        setSeedrFolderContentsCache(prev => {
          const cached = prev[file.folderId];
          if (!cached) return prev;
          const remaining = cached.filter(item => item.id !== file.id);
          const next = { ...prev };
          if (remaining.length > 0) {
            next[file.folderId] = remaining;
          } else {
            delete next[file.folderId];
          }
          return next;
        });
        setSeedrLibraryFolders(prev => prev.map(item =>
          (item.folderId === file.folderId || item.id === file.folderId)
            ? {
                ...item,
                filesCount: Math.max(0, Number(item.filesCount || 0) - 1),
                totalSize: Math.max(0, Number(item.totalSize || 0) - Number(file.size || 0)),
              }
            : item
        ));
      }

      setSeedrError(null);
      setSeedrDeleteNotice('Deleted successfully');
      window.setTimeout(() => setSeedrDeleteNotice(null), 1800);
      const quota = await api.getSeedrQuota().catch(() => null);
      if (quota?.configured) {
        setSeedrQuota({
          maxSpace: quota.maxSpace,
          usedSpace: quota.usedSpace,
          remainingSpace: quota.remainingSpace
        });
      }
    } catch (error) {
      setSeedrDeleteNotice(null);
      console.error('Failed to delete Seedr item:', error);
      setSeedrError(error instanceof Error ? error.message : 'Failed to delete Seedr item');
    }
  };

  const handleDeleteSeedrFolder = async (folderId: string) => {
    setSeedrDeleteNotice('Deleting…');
    try {
      await api.deleteSeedrFolder(folderId);
      setSeedrFiles(prev => prev.filter(item => item.folderId !== folderId));
      setSeedrFolderContentsCache(prev => {
        if (!(folderId in prev)) return prev;
        const next = { ...prev };
        delete next[folderId];
        return next;
      });
      setSeedrLibraryFolders(prev => prev.filter(item => item.folderId !== folderId && item.id !== folderId));
      setSelectedSeedrFolderId(prev => prev === folderId ? null : prev);
      setSeedrError(null);
      setSeedrDeleteNotice('Deleted successfully');
      window.setTimeout(() => setSeedrDeleteNotice(null), 1800);
      const quota = await api.getSeedrQuota().catch(() => null);
      if (quota?.configured) {
        setSeedrQuota({
          maxSpace: quota.maxSpace,
          usedSpace: quota.usedSpace,
          remainingSpace: quota.remainingSpace
        });
      }
    } catch (error) {
      setSeedrDeleteNotice(null);
      console.error('Failed to delete Seedr folder:', error);
      setSeedrError(error instanceof Error ? error.message : 'Failed to delete Seedr folder');
    }
  };

  const handleDeleteFile = (id: string) => {
    const file = files.find(f => f.id === id);
    if (!file) return;
    setDeleteTarget({
      type: 'file',
      id: file.id,
      name: file.name,
      details: `${formatBytes(file.size)} • Folder: ${file.folder}`
    });
  };

  const handleConfirmDelete = async () => {
    if (!deleteTarget) return;
    try {
      if (deleteTarget.type === 'file') {
        await api.deleteFile(deleteTarget.id);
        setFiles(prev => prev.filter(f => f.id !== deleteTarget.id));
        const stats = await api.getStorageStats();
        setStorageStats(stats);
      } else if (deleteTarget.type === 'torrent') {
        await api.deleteTorrent(deleteTarget.id, true);
        setTorrents(prev => prev.filter(t => t.hash !== deleteTarget.id));
        const stats = await api.getStorageStats();
        setStorageStats(stats);
        const f = await api.getFiles(currentFolder);
        setFiles(f);
      }
    } catch (err) {
      console.error('Failed to execute delete:', err);
    }
  };

  const handleCreateFolder = async (name: string, isShared: boolean) => {
    const newFolder = await api.createFolder(name, currentFolder, isShared);
    setFolders(prev => [...prev, newFolder]);
  };

  const handleMoveFile = async (fileId: string, targetFolder: string) => {
    await api.moveFile(fileId, targetFolder);
    const updated = await api.getFiles(currentFolder);
    setFiles(updated);
  };

  const handleRename = async (id: string, newName: string, isFolder: boolean) => {
    await api.renameItem(id, newName, isFolder);
    if (isFolder) {
      const f = await api.getFolders();
      setFolders(f);
    }
    const updated = await api.getFiles(currentFolder);
    setFiles(updated);
  };

  const handleFolderShareSave = async (folderId: string, isShared: boolean, permissions: Record<string, UserPermission>) => {
    await api.updateFolderShare(folderId, isShared, permissions);
    const f = await api.getFolders();
    setFolders(f);
  };

  const handleSwitchUser = async (userId: string) => {
    const { activeUser: newUser } = await api.switchUser(userId);
    setActiveUser(newUser);
  };

  const submitFeedback = async () => {
    const message = feedbackMessage.trim();
    if (message.length < 5) {
      setFeedbackError('Please write at least a few words.');
      return;
    }

    setFeedbackSubmitting(true);
    setFeedbackError('');
    setFeedbackSuccess('');
    try {
      await api.submitFeedback({
        type: feedbackType,
        rating: feedbackType === 'review' ? feedbackRating : undefined,
        message,
        name: feedbackName.trim() || undefined,
      });
      setFeedbackMessage('');
      setFeedbackName('');
      setFeedbackRating(5);
      setFeedbackSuccess('Thanks! Your feedback was sent successfully.');
    } catch (error: any) {
      setFeedbackError(String(error?.message || 'Could not send feedback. Please try again.'));
    } finally {
      setFeedbackSubmitting(false);
    }
  };

  const handleRunCleanup = async () => {
    const res = await api.runCleanup();
    const stats = await api.getStorageStats();
    setStorageStats(stats);
    const f = await api.getFiles(currentFolder);
    setFiles(f);
    const logs = await api.getLogs();
    try {
      const rawLocalLogs = window.localStorage.getItem(activityStorageKey);
      const localLogs = rawLocalLogs ? JSON.parse(rawLocalLogs) : [];
      setActivityLogs(Array.isArray(localLogs) && localLogs.length ? localLogs : logs);
    } catch {
      setActivityLogs(logs);
    }
    return res;
  };

  // Download batch zip
  const activeDownloadsCount = torrents.filter(t => t.state === 'downloading').length;
  const unreadNotifsCount = notifications.filter(n => !n.read).length;

  const currentFolderPrefix = currentFolder === '/' ? '/' : currentFolder + '/';
  const visibleFolders = folders
    .filter(folder => folder.path !== '/' && folder.path.startsWith(currentFolderPrefix))
    .filter(folder => {
      const remainder = folder.path.slice(currentFolderPrefix.length);
      return remainder.length > 0 && !remainder.includes('/');
    });

  if (productWelcomeOpen) {
    return (
      <div className="min-h-screen bg-slate-950 text-slate-100 flex items-center justify-center px-6 py-10">
        <div className="w-full max-w-2xl text-center">
          <div className="mx-auto flex h-20 w-20 items-center justify-center rounded-3xl bg-gradient-to-tr from-cyan-500 via-blue-600 to-indigo-600 shadow-2xl shadow-cyan-500/20">
            <Cloud className="h-10 w-10 text-slate-950 fill-current" />
          </div>

          <p className="mt-8 text-xs font-bold uppercase tracking-[0.3em] text-cyan-400">
            Torrent Studio
          </p>
          <h1 className="mt-4 text-4xl font-black tracking-tight text-white sm:text-6xl">
            Welcome to Torrent Studio
          </h1>
          <p className="mx-auto mt-5 max-w-xl text-base leading-7 text-slate-400 sm:text-lg">
            Search torrents, explore releases, and discover everything you need before deciding where to download.
          </p>

          <button
            type="button"
            onClick={() => {
              try {
                window.localStorage.setItem('torrent_studio_welcome_seen', 'true');
              } catch {}
              setProductWelcomeOpen(false);
            }}
            className="mt-10 inline-flex min-w-44 items-center justify-center rounded-2xl bg-cyan-500 px-7 py-3.5 text-base font-bold text-slate-950 shadow-xl shadow-cyan-500/20 transition hover:bg-cyan-400"
          >
            Start Now
          </button>

          <p className="mt-5 text-xs text-slate-600">
            You can explore search without connecting a Seedr account.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className={`min-h-screen flex flex-col ${theme === 'dark' ? 'bg-slate-950' : 'bg-slate-900'} text-slate-100 transition-colors duration-200`}>
      {/* Top Main Navigation Header */}
      <header className="sticky top-0 z-40 bg-slate-950/90 backdrop-blur-md border-b border-slate-800/80 px-2.5 sm:px-6 py-1.5 sm:py-3">
        <div className="max-w-7xl mx-auto flex items-center justify-between gap-2 sm:gap-3">
          {/* Logo & Brand */}
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-gradient-to-tr from-cyan-500 via-blue-600 to-indigo-600 flex items-center justify-center shadow-lg shadow-cyan-500/25">
              <Cloud className="w-4 h-4 sm:w-5 sm:h-5 text-slate-950 font-black fill-current" />
            </div>
            <div>
              <div className="flex items-center gap-1.5 sm:gap-2">
                <h1 className="text-sm sm:text-lg font-black tracking-tight text-white">Torrent Studio</h1>
</div>
              <p className="text-[10px] text-slate-400 hidden sm:block">
                Unlimited Cloud Seedbox & Media Streamer
              </p>
            </div>
          </div>

          {/* Primary navigation stays on the same header row to save vertical space. */}
          <nav className="hidden md:flex items-center gap-1.5 ml-5 mr-auto">
            <button
              onClick={() => setActiveTab('search')}
              className={activeTab === 'search'
                ? 'px-3.5 py-1.5 rounded-xl text-xs font-semibold flex items-center gap-2 transition bg-cyan-500 text-slate-950 shadow-md shadow-cyan-500/20'
                : 'px-3.5 py-1.5 rounded-xl text-xs font-semibold flex items-center gap-2 transition text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'}
            >
              <Search className="w-4 h-4" />
              <span>Search</span>
            </button>

            <button
              onClick={() => setActiveTab('files')}
              className={activeTab === 'files'
                ? 'px-3.5 py-1.5 rounded-xl text-xs font-semibold flex items-center gap-2 transition bg-cyan-500 text-slate-950 shadow-md shadow-cyan-500/20'
                : 'px-3.5 py-1.5 rounded-xl text-xs font-semibold flex items-center gap-2 transition text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'}
            >
              <Folder className="w-4 h-4" />
              <span>My Cloud Files</span>
              <span className="text-[10px] opacity-70">({files.length})</span>
            </button>
          </nav>

          {/* Right: Quick actions & User Switcher */}
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => {
                setSeedrPat('');
                setSeedrConnectError('');
                setSeedrOnboardingOpen(true);
              }}
              className={seedrConnected
                ? "px-2.5 sm:px-4 py-1.5 sm:py-2 rounded-lg sm:rounded-xl bg-emerald-500/15 hover:bg-emerald-500/25 border border-emerald-500/30 text-emerald-300 text-xs sm:text-sm font-bold flex items-center gap-1.5 shadow-lg shadow-emerald-500/10 transition tap-target"
                : "px-2.5 sm:px-4 py-1.5 sm:py-2 rounded-lg sm:rounded-xl bg-gradient-to-r from-emerald-500 to-cyan-500 hover:from-emerald-400 hover:to-cyan-400 text-slate-950 text-xs sm:text-sm font-bold flex items-center gap-1.5 shadow-lg shadow-emerald-500/20 transition tap-target"}
              title={seedrConnected ? "Seedr is connected" : "Connect to Seedr"}
            >
              {seedrConnected ? <CheckCircle2 className="w-3.5 h-3.5 sm:w-4 sm:h-4" /> : <Cloud className="w-3.5 h-3.5 sm:w-4 sm:h-4" />}
              <span>{seedrConnected ? 'Seedr is Connected' : 'Connect to Seedr'}</span>
            </button>

            {/* Notification Bell */}
            <button
              onClick={() => setIsNotificationsOpen(true)}
              className="p-1.5 sm:p-2 rounded-lg sm:rounded-xl bg-slate-900 hover:bg-slate-800 text-slate-300 relative transition tap-target flex items-center justify-center border border-slate-800"
              title="Notifications & Push Alerts"
            >
              <Bell className="w-3.5 h-3.5 sm:w-4 sm:h-4" />
              {unreadNotifsCount > 0 && (
                <span className="absolute -top-1 -right-1 w-4 h-4 rounded-full bg-cyan-500 text-slate-950 font-bold text-[9px] flex items-center justify-center">
                  {unreadNotifsCount}
                </span>
              )}
            </button>

            {/* Theme Toggle */}
            <button
              onClick={() => setTheme(theme === 'dark' ? 'dim' : 'dark')}
              className="p-2 rounded-xl bg-slate-900 hover:bg-slate-800 text-slate-300 transition tap-target hidden sm:flex items-center justify-center border border-slate-800"
              title={`Theme: ${theme}`}
            >
              <Moon className="w-4 h-4 text-cyan-400" />
            </button>

          </div>
        </div>
      </header>

      {/* Main Content Area */}
      <main className="flex-1 max-w-7xl w-full mx-auto p-2.5 sm:p-6 pb-20 md:pb-12">
        {seedrAddBlockedNotice && (
          <div className="mb-2.5 sm:mb-4 p-2.5 sm:p-3.5 rounded-xl sm:rounded-2xl bg-amber-500/10 border border-amber-500/30 text-amber-200 text-xs flex items-start gap-2.5 shadow-lg">
            <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
            <div className="min-w-0">
              <div className="font-bold text-amber-300">Cannot add another Seedr download</div>
              <div className="mt-0.5 text-amber-200/80">{seedrAddBlockedNotice}</div>
            </div>
          </div>
        )}

        {/* TAB 0: TORRENT SEARCH
            Keep this component mounted when switching tabs so an in-flight
            search continues in the background and its results remain available
            when the user returns to Search. */}
        <div className={activeTab === 'search' ? 'block' : 'hidden'}>
          <TorrentSearchPanel
            onPrepare={handleSearchPrepare}
            onCancelPrepare={handleCancelSeedrDownload}
            onOpenProgress={() => {
              setActiveTab('files');
            }}
            seedrFiles={seedrAllPrefetchedFiles}
            seedrDeletedFolderIds={seedrDeletedFolderIds}
            onPlaySeedrFile={handleStreamSeedrFile}
          />

          {seedrInsufficientSpacePrompt && (
            <div className="fixed inset-x-3 top-20 z-[100] flex justify-center pointer-events-none">
              <div className="w-full max-w-md rounded-2xl border border-rose-400/40 bg-slate-950/95 backdrop-blur-xl shadow-[0_0_30px_rgba(244,63,94,0.22)] p-4 pointer-events-auto">
                <div className="flex items-start gap-3">
                  <div className="shrink-0 rounded-xl bg-rose-500/10 border border-rose-500/20 p-2">
                    <AlertTriangle className="w-5 h-5 text-rose-400" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="font-bold text-rose-300 text-sm">Seedr storage is full</div>
                    <div className="mt-1 text-xs leading-5 text-slate-300">
                      This torrent needs <span className="font-semibold text-slate-100">{formatBytes(seedrInsufficientSpacePrompt.requiredBytes)}</span>,
                      but only <span className="font-semibold text-rose-300">{formatBytes(seedrInsufficientSpacePrompt.remainingBytes)}</span> is available.
                    </div>
                    <div className="mt-3 flex gap-2">
                      <button
                        type="button"
                        onClick={() => {
                          const prompt = seedrInsufficientSpacePrompt;
                          setSeedrInsufficientSpacePrompt(null);
                          void handleAddMagnet(
                            prompt.magnet,
                            prompt.category,
                            prompt.selectedFiles,
                            prompt.manifest,
                            prompt.existingHash,
                            'qbittorrent'
                          );
                        }}
                        className="flex-1 rounded-xl bg-cyan-500 hover:bg-cyan-400 text-slate-950 px-3 py-2 text-xs font-bold transition"
                      >
                        Use qBittorrent
                      </button>
                      <button
                        type="button"
                        onClick={() => setSeedrInsufficientSpacePrompt(null)}
                        className="rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 px-3 py-2 text-xs font-semibold transition"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* TAB 2: MY CLOUD FILES */}
        {activeTab === 'files' && (
          <div className="space-y-2.5 sm:space-y-4">
            {/* Persistent Seedr Library */}
            <div className="p-2.5 sm:p-4 rounded-xl sm:rounded-2xl bg-emerald-500/5 border border-emerald-500/20">
              {seedrDeleteNotice && (
                <div className="mb-3 flex items-center gap-2 rounded-lg border border-emerald-500/20 bg-emerald-500/5 px-3 py-2 text-xs text-emerald-300">
                  <CheckCircle2 className="w-4 h-4" />
                  <span>{seedrDeleteNotice}</span>
                </div>
              )}
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="text-base font-bold text-slate-100 flex items-center gap-2">
                    <Cloud className="w-5 h-5 text-emerald-400" />
                    <span>Seedr Library</span>
                    {seedrConfigured && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-300 border border-emerald-500/20">
                        {seedrLibraryRoot?.filesCount || 0} files
                      </span>
                    )}
                  </h2>
                  <p className="hidden sm:block text-xs text-slate-400 mt-0.5">
                    Files already downloaded to your Seedr account stay visible here, even after refreshing Torrent Studio.
                  </p>
                  {seedrConfigured && (
                    seedrQuota ? (
                      <>                      <div className="grid mt-3 grid-cols-3 gap-2 max-w-xl">
                        <div className="rounded-lg bg-slate-900/80 border border-slate-800 px-3 py-2">
                          <div className="text-[10px] uppercase tracking-wide text-slate-500">Consumed</div>
                          <div className="text-sm font-bold text-slate-100 mt-0.5">{formatBytes(seedrQuota.usedSpace)}</div>
                        </div>
                        <div
                          className={`rounded-lg px-3 py-2 border transition-all duration-300 ${
                            seedrQuota.remainingSpace > 0 && seedrQuota.remainingSpace < 500 * 1024 * 1024
                              ? 'border-rose-400/70 bg-rose-400/10 ring-1 ring-rose-400/30 shadow-[0_0_18px_rgba(244,63,94,0.24)]'
                              : seedrQuota.maxSpace > 0 && seedrQuota.remainingSpace / seedrQuota.maxSpace <= 0.3
                                ? 'border-amber-400/60 bg-amber-400/10 ring-1 ring-amber-400/25 shadow-[0_0_18px_rgba(251,191,36,0.18)]'
                                : 'bg-slate-900/80 border-slate-800'
                          }`}
                        >
                          <div className={`text-[10px] uppercase tracking-wide ${
                            seedrQuota.remainingSpace > 0 && seedrQuota.remainingSpace < 500 * 1024 * 1024
                              ? 'text-rose-300'
                              : seedrQuota.maxSpace > 0 && seedrQuota.remainingSpace / seedrQuota.maxSpace <= 0.3
                                ? 'text-amber-300'
                                : 'text-slate-500'
                          }`}>Remaining</div>
                          <div className={`text-sm font-bold mt-0.5 ${
                            seedrQuota.remainingSpace <= 0
                              ? 'text-rose-300'
                              : seedrQuota.remainingSpace < 500 * 1024 * 1024
                                ? 'text-rose-300'
                                : seedrQuota.maxSpace > 0 && seedrQuota.remainingSpace / seedrQuota.maxSpace <= 0.3
                                  ? 'text-amber-300'
                                  : 'text-emerald-300'
                          }`}>
                            {formatQuotaBytes(seedrQuota.remainingSpace)}
                          </div>
                        </div>
                        <div className="rounded-lg bg-slate-900/80 border border-slate-800 px-3 py-2">
                          <div className="text-[10px] uppercase tracking-wide text-slate-500">Total</div>
                          <div className="text-sm font-bold text-slate-100 mt-0.5">{formatBytes(seedrQuota.maxSpace)}</div>
                        </div>
                      </div>


                    </>
                    ) : null
                  )}
                </div>
                <button
                  type="button"
                  onClick={loadSeedrLibrary}
                  disabled={seedrLoading}
                  className="shrink-0 px-2 sm:px-3 py-1.5 rounded-lg sm:rounded-xl bg-slate-800 hover:bg-slate-700 disabled:opacity-50 text-slate-200 text-xs font-semibold flex items-center gap-1.5 transition"
                >
                  <RefreshCw className={`w-3.5 h-3.5 ${seedrLoading ? 'animate-spin' : ''}`} />
                  <span>{seedrLoading ? 'Refreshing...' : 'Refresh'}</span>
                </button>
              </div>

              {seedrConfigured && seedrQuota && seedrQuota.remainingSpace <= 0 && (
                <div className="mt-3 rounded-xl bg-rose-500/10 border border-rose-500/20 px-3 py-2.5 text-xs text-rose-200">
                  <strong>Seedr is full.</strong> New torrents that fit the Seedr size limit will be offered to qBittorrent instead, or you can free space in Seedr and try again.
                </div>
              )}

              {seedrError && (
                <div className="mt-3 rounded-xl bg-rose-500/10 border border-rose-500/20 px-3 py-2 text-xs text-rose-300">
                  {seedrError}
                </div>
              )}

              {seedrQuotaError && !seedrError && (
                <div className="mt-3 rounded-xl bg-amber-500/10 border border-amber-500/20 px-3 py-2 text-xs text-amber-200">
                  {seedrQuotaError}
                </div>
              )}

              {!seedrLoading && !seedrError && !seedrConfigured && (
                <div className="mt-3 rounded-xl bg-slate-900/70 border border-slate-800 px-3 py-3 text-xs text-slate-400">
                  Seedr is not configured on the server.
                </div>
              )}

              {!seedrLoading && !seedrError && seedrConfigured && (seedrLibraryRoot?.filesCount || 0) === 0 && seedrLibraryFolders.length === 0 && !(seedrNotice?.taskId != null && seedrNotice.status !== 'completed') && (
                <div className="mt-3 rounded-xl bg-slate-900/70 border border-slate-800 px-3 py-3 text-xs text-slate-400">
                  No completed files are currently visible in your Seedr library.                </div>
              )}

              {seedrConfigured && seedrPrefetchLoading && seedrAllPrefetchedFiles.length === 0 && (
                <div className="mt-3 rounded-xl border border-cyan-500/20 bg-cyan-500/5 px-3 py-3 text-xs text-slate-400">
                  <div className="flex items-center gap-2">
                    <Loader2 className="h-4 w-4 animate-spin text-cyan-400" />
                    <span>Loading your Seedr files…</span>
                  </div>
                </div>
              )}


            {/* Header & Breadcrumb & Search */}
            <div className="flex flex-col gap-2 p-2.5 sm:p-4 rounded-xl sm:rounded-2xl bg-slate-900 border border-slate-800">
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                {/* Folder Breadcrumb */}
                <div className="flex items-center gap-2 overflow-x-auto text-xs font-semibold">
                  <button
                    onClick={() => setCurrentFolder('/')}
                    className={`px-2.5 py-1.5 rounded-lg transition ${
                      currentFolder === '/'
                        ? 'bg-cyan-500/20 text-cyan-400'
                        : 'text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    Root
                  </button>

                </div>


              </div>

              {/* Search & Category Filter */}
              <div className="flex flex-col sm:flex-row items-center gap-2.5 pt-2 border-t border-slate-800/80">
                <div className="relative flex-1 w-full">
                  <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
                  <input
                    type="text"
                    placeholder="Search files by name..."
                    value={fileSearch}
                    onChange={(e) => setFileSearch(e.target.value)}
                    className="w-full pl-9 pr-3 py-2 sm:py-1.5 rounded-lg sm:rounded-xl bg-slate-950 border border-slate-800 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-cyan-500"
                  />
                </div>

                <div className="flex items-center gap-1 overflow-x-auto w-full sm:w-auto text-xs">
                  {['all', 'video', 'audio', 'document', 'archive'].map((t) => (
                    <button
                      key={t}
                      onClick={() => setFileTypeFilter(t)}
                      className={`px-2.5 py-1 rounded-lg capitalize font-medium transition ${
                        fileTypeFilter === t
                          ? 'bg-cyan-500 text-slate-950 font-bold'
                          : 'bg-slate-850 text-slate-400 hover:text-slate-200'
                      }`}
                    >
                      {t}
                    </button>
                  ))}
                </div>              </div>
            </div>

            {/* Folders and Files */}
                {visibleFolders.length > 0 ? (
                  <div className="grid grid-cols-1 gap-2.5">
                    {visibleFolders
                      .map((folder) => (
                        <button
                          key={folder.id}                          type="button"
                          onClick={() => setCurrentFolder(folder.path)}
                          className="w-full p-3.5 rounded-2xl bg-slate-900 border border-slate-800 hover:border-cyan-500/40 hover:bg-slate-900/80 transition shadow-sm flex items-center gap-3 text-left group"
                        >
                          <div className="p-2.5 rounded-xl bg-cyan-500/10 border border-cyan-500/20 shrink-0">
                            <Folder className="w-5 h-5 text-cyan-400" />
                          </div>
                          <div className="truncate flex-1">
                            <h4 className="text-sm font-semibold text-slate-200 truncate group-hover:text-cyan-400 transition">
                              {folder.name}
                            </h4>
                            <p className="text-[11px] text-slate-500 mt-0.5">
                              {folder.filesCount || 0} files • {formatBytes(folder.totalSize || 0)}
                            </p>
                          </div>
                          <ChevronRight className="w-4 h-4 text-slate-600 group-hover:text-cyan-400 shrink-0" />
                        </button>
                      ))}
                  </div>
                ) : null}

                {visibleFiles.length > 0 && (
                  <div className="grid grid-cols-1 gap-2.5">
                    {visibleFiles.map((file) => (
                      <FileCard
                        key={file.id}
                        file={file}
                        onPlay={(f) => {
                          // Root "My Cloud Files" rows are Seedr files too, but
                          // they must use the same resolver as the working folder
                          // view so Vercel + Render always targets the backend.
                          if (
                            f.ownerId === 'seedr' &&
                            (f.type === 'video' || f.type === 'audio')
                          ) {
                            void handleStreamSeedrFile({
                              id: f.id,
                              streamId: f.streamId || f.id,
                              name: f.name,
                              size: f.size,
                              folderId: '',
                              folderPath: f.folder || '/'
                            });
                            return;
                          }

                          setActiveMediaFile(f);
                          setIsPlayerMinimized(false);
                        }}
                        streamLoading={file.ownerId === 'seedr' && seedrStreamLoadingId === file.id}
                        onRename={(f) => setRenameItem({ id: f.id, name: f.name, isFolder: false })}
                        onMove={(f) => setMoveFile(f)}
                        onSeedrDelete={file.ownerId === 'seedr'
                          ? (f) => {
                              const seedrFile = seedrAllPrefetchedFiles.find(item => item.id === f.id);
                              if (!seedrFile) {
                                setSeedrError('Seedr file is no longer available. Refresh the library and try again.');
                                return;
                              }
                              return handleDeleteSeedrFile(seedrFile);
                            }
                          : undefined}
                        canEdit={activeUser?.role !== 'viewer'}
                        canDelete={file.ownerId === 'seedr' ? true : activeUser?.role === 'admin'}
                      />
                    ))}
                  </div>
                )}

                {seedrPrefetchLoading && currentFolder === '/' && seedrAllPrefetchedFiles.length === 0 ? (
                  <div className="py-10 text-center rounded-2xl bg-slate-900 border border-slate-800 p-8">
                    <RefreshCw className="w-8 h-8 text-emerald-400 mx-auto mb-3 animate-spin" />
                    <h3 className="text-sm font-bold text-slate-300">Loading Seedr files…</h3>
                    <p className="text-xs text-slate-500 mt-1">Folder metadata is ready. Loading file details in the background.</p>
                  </div>
                ) : visibleFolders.length === 0 && visibleFiles.length === 0 && (
                  <div className="py-16 text-center rounded-2xl bg-slate-900 border border-slate-800 p-8">
                    <Folder className="w-12 h-12 text-slate-700 mx-auto mb-3" />
                    <h3 className="text-sm font-bold text-slate-300">No files found in this folder</h3>
                    <p className="text-xs text-slate-500 mt-1">
                      Completed torrent downloads and uploaded media appear here instantly.
                    </p>
                  </div>
                )}

                {backgroundMetadataJob && (
                  <div className={`mt-3 rounded-xl border px-3 py-3 ${backgroundMetadataJob.error ? 'border-rose-500/30 bg-rose-500/10' : backgroundMetadataJob.ready ? 'border-emerald-500/30 bg-emerald-500/10' : 'border-cyan-500/30 bg-cyan-500/10'}`}>
                    <div className="flex items-center gap-3">
                      <Loader2 className={`w-5 h-5 shrink-0 ${backgroundMetadataJob.ready ? 'text-emerald-400' : backgroundMetadataJob.error ? 'text-rose-400' : 'text-cyan-400 animate-spin'}`} />
                      <div className="min-w-0 flex-1">
                        <div className="text-xs font-bold text-slate-100">{backgroundMetadataJob.title}</div>
                        <div className="text-[11px] text-slate-400 mt-0.5">{backgroundMetadataJob.message}</div>
                      </div>
                      <button
                        type="button"
                        onClick={() => {
                          const source = backgroundMetadataJob.hash
                            ? 'magnet:?xt=urn:btih:' + backgroundMetadataJob.hash
                            : '';
                          openMetadataSelector(source);
                          setActiveTab('files');
                        }}
                        className="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-[11px] font-semibold"
                      >
                        Open
                      </button>
                      <button
                        type="button"
                        onClick={() => setBackgroundMetadataJob(null)}
                        className="p-1 rounded-lg text-slate-500 hover:text-slate-200"
                        aria-label="Dismiss metadata status"
                      >
                        ×
                      </button>
                    </div>
                  </div>
                )}

                {seedrDownloadActive && seedrNotice && (
                  <div className="mt-3 rounded-xl border border-emerald-500/25 bg-emerald-500/5 px-3.5 py-3">
                    <div className="flex items-center gap-3">
                      <Loader2 className="h-5 w-5 shrink-0 animate-spin text-emerald-400" />
                      <div className="min-w-0 flex-1">
                        <div className="text-xs font-bold text-slate-100 truncate">Loading {seedrNotice.name || 'torrent'} in Seedr</div>
                        <div className="mt-0.5 text-[11px] text-slate-400">{Number(seedrNotice.progress || 0).toFixed(1)}% complete</div>
                        <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
                          <div className="h-full rounded-full bg-emerald-400 transition-all duration-500" style={{ width: Math.max(0, Math.min(100, Number(seedrNotice.progress) || 0)) + '%' }} />
                        </div>
                      </div>
                      <button type="button" onClick={() => void handleCancelSeedrDownload()} disabled={isCancellingSeedr} className="shrink-0 rounded-lg border border-rose-500/25 bg-rose-500/10 px-2.5 py-1.5 text-[10px] font-bold text-rose-300 hover:bg-rose-500/20 disabled:opacity-50">
                        {isCancellingSeedr ? 'Cancelling…' : 'Cancel'}
                      </button>
                    </div>
                  </div>
                )}

              </div>
            </div>
        )}

        {/* TAB 3: SHARED STORAGE & MULTI-USER */}
        {activeTab === 'shared' && (
          <div className="space-y-4">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-4 rounded-2xl bg-slate-900 border border-slate-800">
              <div>
                <h2 className="text-base font-bold text-slate-100 flex items-center gap-2">
                  <Users className="w-5 h-5 text-cyan-400" />
                  <span>Shared Team Folders & Access Controls</span>
                </h2>
                <p className="text-xs text-slate-400 mt-0.5">
                  Multi-user folder permissions with customizable Viewer, Editor, and Admin roles.
                </p>
              </div>

              <button
                onClick={() => setIsCreateFolderOpen(true)}
                className="px-3.5 py-2 rounded-xl bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold text-xs flex items-center gap-1.5 transition"
              >
                <FolderPlus className="w-4 h-4" />
                <span>New Shared Folder</span>
              </button>
            </div>

            {/* Folders List */}
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {folders.filter(f => f.path !== '/').map((folder) => {
                const isOwner = folder.ownerId === activeUser?.id;
                const userPerm = isOwner ? 'admin' : folder.permissions[activeUser?.id || ''] || 'viewer';

                  return (
                  <div
                    key={folder.id}
                    className="p-4 rounded-2xl bg-slate-900 border border-slate-800 hover:border-slate-700 transition flex flex-col justify-between gap-3"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="flex items-center gap-2.5 overflow-hidden">
                        <div className="p-2.5 rounded-xl bg-cyan-500/10 text-cyan-400 shrink-0">
                          <Folder className="w-[18px] h-[18px]" />
                        </div>
                        <div className="truncate">
                          <h4 className="text-sm font-semibold text-slate-100 truncate">{folder.name}</h4>
                          <p className="text-[11px] text-slate-400 mt-0.5">
                            Created by {folder.ownerName}
                          </p>
                        </div>
                      </div>

                      <span
                        className={`text-[10px] font-bold px-2 py-0.5 rounded-full uppercase ${
                          folder.isShared                            ? 'bg-cyan-500/10 text-cyan-400 border border-cyan-500/20'
                            : 'bg-slate-800 text-slate-400'
                        }`}
                      >
                        {folder.isShared ? 'Shared' : 'Private'}
                      </span>
                    </div>

                    <div className="flex items-center justify-between text-xs text-slate-400 pt-2 border-t border-slate-800/60">
                      <span>{folder.filesCount || 0} files ({formatBytes(folder.totalSize || 0)})</span>
                      <span className="text-cyan-400 font-medium capitalize">Role: {userPerm}</span>                    </div>

                    <div className="flex items-center gap-2 pt-1">
                      <button
                        onClick={() => {
                          setCurrentFolder(folder.path);
                          setActiveTab('files');
                        }}
                        className="flex-1 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold transition"
                      >
                        Open Folder
                      </button>

                      <button
                        onClick={() => setShareFolder(folder)}
                        className="p-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-cyan-400 transition"
                        title="Manage Permissions"
                      >
                        <Share2 className="w-4 h-4" />
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
        {/* Activity Log and Auto-Cleanup/Disk tabs are intentionally hidden. */}

      </main>

      {/* Floating Bottom Media Player (when minimized or active) */}
      <MediaPlayerModal
        file={activeMediaFile}
        onClose={() => {
          setActiveMediaFile(null);
          setSeedrStreamLoadingId(null);
        }}
        onPlaybackStarted={() => {
          // Stage 1 and stage 2 both end only when the browser actually
          // starts playback. A fast stream therefore removes the spinner
          // immediately, while a slow stream keeps it visible.
          setSeedrStreamLoadingId(null);
        }}
        isMinimized={isPlayerMinimized}
        onToggleMinimize={() => setIsPlayerMinimized(!isPlayerMinimized)}
      />

      {/* Mobile Bottom Navigation */}
      <nav className="md:hidden fixed bottom-0 left-0 right-0 z-50 bg-slate-950/95 backdrop-blur-xl border-t border-slate-800 px-1 pb-[calc(env(safe-area-inset-bottom)+2px)] pt-1">
        <div className="grid grid-cols-3 items-center">
          <button
            onClick={() => { setActiveTab('search'); }}
            className={`flex flex-col items-center justify-center gap-0.5 min-h-11 px-1 rounded-lg transition ${activeTab === 'search' ? 'text-cyan-400' : 'text-slate-400'}`}
          >
            <Search className="w-[18px] h-[18px]" />
            <span className="text-[9px] font-semibold">Search</span>
          </button>

          <button
            onClick={() => { setActiveTab('files'); }}
            className={`flex flex-col items-center justify-center gap-0.5 min-h-12 px-1 rounded-xl transition ${activeTab === 'files' ? 'text-cyan-400' : 'text-slate-400'}`}
          >
            <Folder className="w-5 h-5" />
            <span className="text-[9px] font-semibold">Files</span>
          </button>

          <button
            type="button"
            onClick={() => {
              setFeedbackOpen(true);
              setFeedbackSuccess('');
              setFeedbackError('');
            }}
            className={`flex flex-col items-center justify-center gap-0.5 min-h-12 px-1 rounded-xl transition ${feedbackOpen ? 'text-cyan-400' : 'text-slate-400'}`}
          >
            <MessageSquare className="w-5 h-5" />
            <span className="text-[9px] font-semibold">Feedback</span>
          </button>
        </div>
      </nav>

      {/* Modals */}
      <AddMagnetModal
        isOpen={isAddMagnetOpen}
        onClose={() => {
          setIsAddMagnetOpen(false);
          setInitialMagnet('');
          setInitialSourceUrl('');
          setInitialDescriptorUrl('');
        }}
        onOpen={() => {
          openAddMagnet(initialMagnet);
        }}
        onAdd={handleAddMagnet}
        onBackgroundChange={(state) => {
          setBackgroundMetadataJob(state);
          if (state.active) {
            setActiveTab('files');
            setIsAddMagnetOpen(false);
          }
        }}
        defaultFolder={currentFolder === '/' ? 'Downloads' : currentFolder.replace('/', '')}
        initialMagnet={initialMagnet}
        initialSourceUrl={initialSourceUrl}
        initialDescriptorUrl={initialDescriptorUrl}
      />

      <FilePrioModal
        torrent={prioTorrent}
        onClose={() => setPrioTorrent(null)}
        onUpdatePriority={handleUpdateFilePriority}
      />

      <StorageCleanupModal
        isOpen={isCleanupOpen}
        onClose={() => setIsCleanupOpen(false)}
        stats={storageStats}
        settings={cleanupSettings}
        onUpdateSettings={async (settings) => {
          const updated = await api.updateCleanupSettings(settings);
          setCleanupSettings(updated);
        }}
        onRunCleanup={handleRunCleanup}
      />

      <FolderShareModal
        folder={shareFolder}
        users={users}
        onClose={() => setShareFolder(null)}
        onSave={handleFolderShareSave}
      />

      <NotificationCenter
        notifications={notifications}
        isOpen={isNotificationsOpen}
        onClose={() => setIsNotificationsOpen(false)}
        onMarkRead={async () => {
          await api.markNotificationsRead();
          setNotifications(prev => prev.map(n => ({ ...n, read: true })));
        }}
        onTestPush={async () => {
          await api.testNotification();
          dispatchBrowserNotification('Torrent Studio Push Notification Test', 'Push alert successfully triggered! Everything is running smoothly.');
          const notifs = await api.getNotifications();
          setNotifications(notifs);
        }}      />

      <CreateFolderModal
        isOpen={isCreateFolderOpen}
        onClose={() => setIsCreateFolderOpen(false)}
        onCreate={handleCreateFolder}
        currentPath={currentFolder}
      />

      <MoveFileModal
        file={moveFile}
        folders={folders}        onClose={() => setMoveFile(null)}
        onMove={handleMoveFile}
      />

      <RenameModal
        item={renameItem}
        onClose={() => setRenameItem(null)}
        onRename={handleRename}
      />

      {deleteTarget && (
        <ConfirmDeleteModal
          isOpen={Boolean(deleteTarget)}
          title={deleteTarget.type === 'file' ? 'Delete File' : 'Remove Torrent Task'}
          itemName={deleteTarget.name}
          itemDetails={deleteTarget.details}
          itemType={deleteTarget.type}
          onConfirm={handleConfirmDelete}
          onClose={() => setDeleteTarget(null)}
        />
      )}
      {feedbackOpen && (
        <div
          className="fixed inset-0 z-[110] flex items-center justify-center bg-slate-950/70 px-4 py-6 backdrop-blur-md"
          role="dialog"
          aria-modal="true"
          aria-labelledby="feedback-title"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setFeedbackOpen(false);
          }}
        >
          <div
            className="relative w-full max-w-md rounded-2xl border border-cyan-500/20 bg-slate-900 p-5 shadow-2xl sm:p-6"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <button
              type="button"
              aria-label="Close feedback"
              onClick={() => setFeedbackOpen(false)}
              className="absolute right-3 top-3 flex h-9 w-9 items-center justify-center rounded-full text-slate-400 transition hover:bg-slate-800 hover:text-white"
            >
              <span className="text-2xl leading-none">×</span>
            </button>

            <div className="flex items-start gap-3 pr-8">
              <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-cyan-500/10 text-cyan-400">
                <MessageSquare className="h-6 w-6" />
              </div>
              <div>
                <h2 id="feedback-title" className="text-lg font-bold text-slate-100">Feedback</h2>
                <p className="mt-1 text-sm leading-5 text-slate-400">
                  Tell us what you think, suggest an improvement, or report a problem.
                </p>
              </div>
            </div>

            <div className="mt-5 grid grid-cols-3 gap-2">
              {([
                ['review', 'Review'],
                ['suggestion', 'Suggestion'],
                ['bug', 'Problem'],
              ] as const).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setFeedbackType(value)}
                  className={feedbackType === value
                    ? 'rounded-xl bg-cyan-500 px-3 py-2.5 text-xs font-bold text-slate-950'
                    : 'rounded-xl border border-slate-700 bg-slate-800 px-3 py-2.5 text-xs font-semibold text-slate-300 hover:bg-slate-700'}
                >
                  {label}
                </button>
              ))}
            </div>

            {feedbackType === 'review' && (
              <div className="mt-4">
                <p className="text-xs font-semibold text-slate-300">Rating</p>
                <div className="mt-2 flex gap-1.5">
                  {[1, 2, 3, 4, 5].map(value => (
                    <button
                      key={value}
                      type="button"
                      onClick={() => setFeedbackRating(value)}
                      aria-label={`${value} star${value === 1 ? '' : 's'}`}
                      className={value <= feedbackRating ? 'text-amber-400 text-2xl leading-none' : 'text-slate-700 text-2xl leading-none'}
                    >
                      ★
                    </button>
                  ))}
                </div>
              </div>
            )}

            <textarea
              value={feedbackMessage}
              onChange={event => setFeedbackMessage(event.target.value)}
              placeholder={feedbackType === 'review' ? 'How is Torrent Studio for you?' : feedbackType === 'suggestion' ? 'What would you like us to add or improve?' : 'What went wrong?'}
              maxLength={3000}
              rows={5}
              className="mt-4 w-full resize-none rounded-xl border border-slate-700 bg-slate-950 px-3.5 py-3 text-sm text-slate-100 outline-none placeholder:text-slate-600 focus:border-cyan-500/50 focus:ring-2 focus:ring-cyan-500/10"
            />

            <input
              value={feedbackName}
              onChange={event => setFeedbackName(event.target.value)}
              maxLength={80}
              placeholder="Name (optional)"
              className="mt-3 w-full rounded-xl border border-slate-700 bg-slate-950 px-3.5 py-3 text-sm text-slate-100 outline-none placeholder:text-slate-600 focus:border-cyan-500/50 focus:ring-2 focus:ring-cyan-500/10"
            />

            {feedbackError && (
              <div className="mt-3 rounded-xl border border-rose-500/25 bg-rose-500/10 p-3 text-xs leading-5 text-rose-200">
                {feedbackError}
              </div>
            )}
            {feedbackSuccess && (
              <div className="mt-3 rounded-xl border border-emerald-500/25 bg-emerald-500/10 p-3 text-xs leading-5 text-emerald-200">
                {feedbackSuccess}
              </div>
            )}

            <button
              type="button"
              disabled={feedbackSubmitting || feedbackMessage.trim().length < 5}
              onClick={() => void submitFeedback()}
              className="mt-4 w-full rounded-xl bg-cyan-500 px-4 py-3 text-sm font-bold text-slate-950 transition hover:bg-cyan-400 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {feedbackSubmitting ? 'Sending…' : 'Send Feedback'}
            </button>

            <p className="mt-3 text-center text-[11px] leading-4 text-slate-600">
              Your feedback is reviewed by the Torrent Studio team.
            </p>
          </div>
        </div>
      )}

      {seedrOnboardingOpen && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center overflow-y-auto bg-slate-950/50 px-4 py-6 backdrop-blur-[3px]"
          role="dialog"
          aria-modal="true"
          aria-labelledby="seedr-onboarding-title"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              setSeedrOnboardingOpen(false);
            }
          }}
        >
          <div
            className="relative my-auto w-full max-w-lg rounded-2xl border border-emerald-500/25 bg-slate-900/95 p-5 shadow-2xl shadow-emerald-500/10 sm:p-6"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <button
              type="button"
              aria-label="Close Seedr connection dialog"
              onClick={() => setSeedrOnboardingOpen(false)}
              className="absolute right-3 top-3 flex h-9 w-9 items-center justify-center rounded-full text-slate-400 transition hover:bg-slate-800 hover:text-white"
            >
              <span className="text-2xl leading-none">×</span>
            </button>

            <div className="flex items-start gap-3 pr-8">
              <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-emerald-500/10 text-emerald-400">
                <Cloud className="h-6 w-6" />
              </div>
              <div className="min-w-0">
                <h2 id="seedr-onboarding-title" className="text-lg font-bold text-slate-100">
                  {seedrConnected ? 'Seedr account connected' : 'Connect to Seedr'}
                </h2>
                <p className="mt-1 text-sm leading-5 text-slate-400">
                  {seedrConnected
                    ? 'Torrent Studio is connected to your Seedr account.'
                    : 'Connect your own Seedr account to send torrents directly to your Seedr storage.'}
                </p>
              </div>
            </div>

            {seedrConnected ? (
              <>
                <div className="mt-5 rounded-xl border border-emerald-500/25 bg-emerald-500/10 p-4">
                  <p className="text-sm font-semibold text-emerald-200">Connected successfully</p>
                  <p className="mt-1 text-xs leading-5 text-slate-300">
                    This browser session is using your Seedr account. No shared Seedr storage is used.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setSeedrOnboardingOpen(false)}
                  className="mt-5 w-full rounded-xl bg-emerald-500 px-4 py-2.5 text-sm font-bold text-slate-950 transition hover:bg-emerald-400"
                >
                  Done
                </button>
              </>
            ) : (
              <div className="mt-5 space-y-4">
                <div className="rounded-xl border border-emerald-500/25 bg-emerald-500/10 p-4">
                  <div className="flex items-start gap-3">
                    <ShieldCheck className="mt-0.5 h-5 w-5 shrink-0 text-emerald-400" />
                    <div className="min-w-0">
                      <p className="text-sm font-bold text-emerald-200">Have a Seedr account?</p>
                      <p className="mt-1 text-xs leading-5 text-slate-300">
                        Paste your Seedr token below. Your Seedr password is never requested.
                      </p>
                    </div>
                  </div>

                  <input
                    type="password"
                    value={seedrPat}
                    onChange={(e) => setSeedrPat(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !seedrPatSubmitting && seedrSessionReady) {
                        void connectSeedrWithPat();
                      }
                    }}
                    placeholder="Paste your Seedr token"
                    autoComplete="off"
                    spellCheck={false}
                    className="mt-4 w-full rounded-xl border border-slate-700 bg-slate-950 px-3.5 py-3 font-mono text-sm text-slate-100 outline-none transition placeholder:text-slate-600 focus:border-emerald-500/50 focus:ring-2 focus:ring-emerald-500/10"
                  />

                  {seedrConnectError && (
                    <div className="mt-3 rounded-xl border border-amber-500/25 bg-amber-500/10 p-3">
                      <p className="text-xs leading-5 text-amber-100">{seedrConnectError}</p>
                    </div>
                  )}

                  <button
                    type="button"
                    disabled={seedrPatSubmitting || !seedrPat.trim() || !seedrSessionReady}
                    onClick={() => { void connectSeedrWithPat(); }}
                    className="mt-3 w-full rounded-xl bg-emerald-500 px-4 py-2.5 text-sm font-bold text-slate-950 transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {seedrPatSubmitting ? (
                      <span className="inline-flex items-center justify-center gap-2">
                        <Loader2 className="h-4 w-4 animate-spin" />
                        Connecting…
                      </span>
                    ) : (
                      'Connect'
                    )}
                  </button>
                </div>

                <div className="rounded-xl border border-slate-800 bg-slate-950/70 p-4">
                  <div className="flex items-center gap-2">
                    <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-cyan-500/10 text-xs font-black text-cyan-300">?</div>
                    <p className="text-sm font-bold text-slate-100">Create your Seedr token</p>
                  </div>

                  <ol className="mt-3 space-y-2.5 pl-5 text-xs leading-5 text-slate-300 list-decimal">
                    <li>
                      Open the{' '}
                      <a
                        href="https://www.seedr.cc/api/v0.1/console/tokens"
                        target="_blank"
                        rel="noreferrer"
                        className="font-semibold text-emerald-300 underline decoration-emerald-500/40 underline-offset-2 hover:text-emerald-200"
                      >
                        Seedr tokens
                      </a>{' '}
                      page.
                    </li>
                    <li>Click <span className="font-semibold text-slate-100">“Google Login”</span>.</li>
                    <li>The token page opens.</li>
                    <li>Token Name = <span className="font-semibold text-slate-100">Any name</span>.</li>
                    <li><span className="font-semibold text-slate-100">Expiration</span> = <span className="font-semibold text-slate-100">Never</span>.</li>
                    <li>Scopes = click <span className="font-semibold text-slate-100">Full Account Access</span>.</li>
                    <li>Go below and click <span className="font-semibold text-slate-100">Generate Token</span>.</li>
                    <li>Copy the token code and save it somewhere safe.</li>
                    <li>Paste the token code above and click <span className="font-semibold text-slate-100">Connect</span>.</li>
                  </ol>
                </div>

                <p className="text-center text-[11px] leading-4 text-slate-500">
                  Your token is sent only to Torrent Studio over HTTPS and is kept in your secure browser session.
                </p>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}