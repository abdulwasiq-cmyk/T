import React from 'react';

type MovieCatalogueCardProps = {
  children: React.ReactNode;
  className?: string;
  onClick?: React.MouseEventHandler<HTMLElement>;
};

/** Shared outer frame for movie catalogue cards. Catalogue-specific artwork,
 * metadata and actions remain in the caller; the common card surface lives here. */
export const MovieCatalogueCard: React.FC<MovieCatalogueCardProps> = ({ children, className = '', onClick }) => (
  <article
    onClick={onClick}
    className={['min-w-0 overflow-hidden rounded-xl border border-slate-800 bg-slate-900 flex flex-col', className].filter(Boolean).join(' ')}
  >
    {children}
  </article>
);
