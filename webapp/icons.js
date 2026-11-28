/* NexaPilot Mini App — Crisp Inline SVG Icon Registry (24x24 viewBox) */
window.NexaIcons = (function () {
  const S = 'fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round"';

  const paths = {
    logo: `<rect x="2" y="2" width="20" height="20" rx="6" fill="url(#npLogoGrad)" stroke="none"/>
      <defs><linearGradient id="npLogoGrad" x1="2" y1="2" x2="22" y2="22" gradientUnits="userSpaceOnUse">
        <stop stop-color="#3B82F6"/><stop offset="1" stop-color="#10B981"/>
      </linearGradient></defs>
      <path d="M7.5 16.5V7.5L16.5 16.5V7.5" stroke="#FFFFFF" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>`,

    dashboard: `<rect x="3" y="3" width="7" height="9" rx="2" ${S}/><rect x="14" y="3" width="7" height="5" rx="2" ${S}/><rect x="14" y="12" width="7" height="9" rx="2" ${S}/><rect x="3" y="16" width="7" height="5" rx="2" ${S}/>`,
    account: `<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2" ${S}/><circle cx="12" cy="7" r="4" ${S}/>`,
    megaphone: `<path d="m3 11 18-5v12L3 14v-3z" ${S}/><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6" ${S}/>`,
    template: `<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" ${S}/><polyline points="14 2 14 8 20 8" ${S}/><line x1="8" y1="13" x2="16" y2="13" ${S}/><line x1="8" y1="17" x2="13" y2="17" ${S}/>`,
    crown: `<path d="m2 7 4.41 4.41A2 2 0 0 0 7.83 12h8.34a2 2 0 0 0 1.42-.59L22 7l-2 12H4L2 7z" ${S}/><path d="M12 4v8" ${S}/><circle cx="12" cy="4" r="1" fill="currentColor"/>`,
    shield: `<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" ${S}/><path d="m9 12 2 2 4-4" ${S}/>`,
    gift: `<rect x="3" y="8" width="18" height="4" rx="1" ${S}/><path d="M12 8v13" ${S}/><path d="M19 12v7a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-7" ${S}/><path d="M7.5 8a2.5 2.5 0 0 1 0-5A4.8 8 0 0 1 12 8a4.8 8 0 0 1 4.5-5 2.5 2.5 0 0 1 0 5" ${S}/>`,
    bolt: `<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" ${S}/>`,
    lock: `<rect x="3" y="11" width="18" height="11" rx="2" ${S}/><path d="M7 11V7a5 5 0 0 1 10 0v4" ${S}/>`,
    pulse: `<polyline points="22 12 18 12 15 21 9 3 6 12 2 12" ${S}/>`,
    logout: `<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" ${S}/><polyline points="16 17 21 12 16 7" ${S}/><line x1="21" y1="12" x2="9" y2="12" ${S}/>`,
    bot: `<rect x="3" y="11" width="18" height="10" rx="2" ${S}/><circle cx="12" cy="5" r="2" ${S}/><path d="M12 7v4" ${S}/><line x1="8" y1="16" x2="8.01" y2="16" stroke-width="2.5" stroke="currentColor" stroke-linecap="round"/><line x1="16" y1="16" x2="16.01" y2="16" stroke-width="2.5" stroke="currentColor" stroke-linecap="round"/>`,
    clock: `<circle cx="12" cy="12" r="9" ${S}/><polyline points="12 7 12 12 15 15" ${S}/>`,
    wallet: `<path d="M21 12V7H5a2 2 0 0 1 0-4h14v4" ${S}/><path d="M3 5v14a2 2 0 0 0 2 2h16v-5" ${S}/><path d="M18 12a2 2 0 0 0 0 4h4v-4h-4z" ${S}/>`,
    card: `<rect x="2" y="5" width="20" height="14" rx="2" ${S}/><line x1="2" y1="10" x2="22" y2="10" ${S}/>`,
    users: `<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" ${S}/><circle cx="9" cy="7" r="4" ${S}/><path d="M22 21v-2a4 4 0 0 0-3-3.87" ${S}/><path d="M16 3.13a4 4 0 0 1 0 7.75" ${S}/>`,
    copy: `<rect x="9" y="9" width="13" height="13" rx="2" ${S}/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" ${S}/>`,
    check: `<polyline points="20 6 9 17 4 12" ${S}/>`,
    trash: `<polyline points="3 6 5 6 21 6" ${S}/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" ${S}/>`,
    play: `<polygon points="6 4 20 12 6 20 6 4" ${S}/>`,
    pause: `<rect x="6" y="4" width="4" height="16" rx="1" ${S}/><rect x="14" y="4" width="4" height="16" rx="1" ${S}/>`,
    stop: `<rect x="5" y="5" width="14" height="14" rx="2" ${S}/>`,
    refresh: `<path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" ${S}/><path d="M3 3v5h5" ${S}/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16" ${S}/><path d="M16 16h5v5" ${S}/>`,
    plus: `<line x1="12" y1="5" x2="12" y2="19" ${S}/><line x1="5" y1="12" x2="19" y2="12" ${S}/>`,
    share: `<circle cx="18" cy="5" r="3" ${S}/><circle cx="6" cy="12" r="3" ${S}/><circle cx="18" cy="19" r="3" ${S}/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49" ${S}/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49" ${S}/>`,
    close: `<line x1="18" y1="6" x2="6" y2="18" ${S}/><line x1="6" y1="6" x2="18" y2="18" ${S}/>`,
    chevron: `<polyline points="9 18 15 12 9 6" ${S}/>`,
    send: `<line x1="22" y1="2" x2="11" y2="13" ${S}/><polygon points="22 2 15 22 11 13 2 9 22 2" ${S}/>`,
    key: `<circle cx="7.5" cy="15.5" r="4.5" ${S}/><path d="m21 2-9.6 9.6" ${S}/><path d="m15.5 7.5 3 3L22 7l-3-3" ${S}/>`,
    alert: `<circle cx="12" cy="12" r="10" ${S}/><line x1="12" y1="8" x2="12" y2="12" ${S}/><line x1="12" y1="16" x2="12.01" y2="16" stroke-width="2.4" stroke="currentColor" stroke-linecap="round"/>`,
  };

  function icon(name, size = 18, cls = "") {
    const inner = paths[name] || paths.bolt;
    return `<svg class="np-icon ${cls}" width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true">${inner}</svg>`;
  }

  return { icon };
})();
