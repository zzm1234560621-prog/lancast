/*
 * Inline line icons (Lucide-style: 24px box, stroke 2, round caps/joins).
 * Everything is a local string; nothing is fetched.
 */

const ICONS = {
  logo: '<rect x="2.5" y="3.5" width="19" height="13" rx="2.5"/><path d="M8.5 21h7M12 16.5V21"/><path d="M10 7.5l4.5 2.5-4.5 2.5z" fill="currentColor" stroke="none"/>',
  monitor: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
  'chevron-down': '<path d="m6 9 6 6 6-6"/>',
  'chevron-up': '<path d="m18 15-6-6-6 6"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  maximize: '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M16 3h3a2 2 0 0 1 2 2v3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/><path d="M8 21H5a2 2 0 0 1-2-2v-3"/>',
  minimize: '<path d="M8 3v3a2 2 0 0 1-2 2H3"/><path d="M16 3v3a2 2 0 0 0 2 2h3"/><path d="M16 21v-3a2 2 0 0 1 2-2h3"/><path d="M8 21v-3a2 2 0 0 0-2-2H3"/>',
  'bar-chart-2': '<path d="M18 20V10M12 20V4M6 20v-6"/>',
  'log-out': '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
  fit: '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M16 3h3a2 2 0 0 1 2 2v3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/><path d="M8 21H5a2 2 0 0 1-2-2v-3"/>',
  crop: '<path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="m21 3-7 7"/><path d="m3 21 7-7"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  power: '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><path d="M12 2v10"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
  'refresh-cw': '<path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10"/><path d="M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
  unplug: '<path d="M9 2v5M15 2v5"/><path d="M7 7h10v3a5 5 0 0 1-10 0z"/><path d="M12 15v7"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/>',
  film: '<rect x="2" y="3" width="20" height="18" rx="2"/><path d="M7 3v18M17 3v18M2 8h5M2 16h5M17 8h5M17 16h5"/>',
  zap: '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
  'alert-circle': '<circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>',
  'wifi-off': '<path d="M1 1l22 22"/><path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55"/><path d="M5 12.55a10.94 10.94 0 0 1 5.17-2.39"/><path d="M10.71 5.05A16 16 0 0 1 22.58 9"/><path d="M1.42 9a15.91 15.91 0 0 1 4.7-2.88"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><path d="M12 20h.01"/>',
  lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  hand: '<path d="M8 12V6a1.5 1.5 0 0 1 3 0v5"/><path d="M11 11V4.5a1.5 1.5 0 0 1 3 0V11"/><path d="M14 11V6a1.5 1.5 0 0 1 3 0v7c0 5-2.5 8-7 8-2 0-3.5-1-5-3l-2.5-4a1.6 1.6 0 0 1 2.6-1.8L8 15z"/>',
  'mouse-pointer': '<path d="m3 3 7.07 16.97 2.51-7.39 7.39-2.51L3 3z"/><path d="m13 13 6 6"/>',
  mouse: '<rect x="5" y="2" width="14" height="20" rx="7"/><path d="M12 2v6"/>',
  keyboard: '<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M6 14h.01M18 14h.01M9 14h6"/>',
  sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  gamepad: '<line x1="6" y1="12" x2="10" y2="12"/><line x1="8" y1="10" x2="8" y2="14"/><circle cx="15" cy="11" r="1"/><circle cx="18" cy="13" r="1"/><path d="M17.32 5H6.68a4 4 0 0 0-3.98 3.59l-.64 6.4A2 2 0 0 0 4.05 18h.38a2 2 0 0 0 1.66-.89l1.32-1.94A2 2 0 0 1 9.07 14h5.86a2 2 0 0 1 1.66.89l1.32 1.94a2 2 0 0 0 1.66.89h.38a2 2 0 0 0 1.99-2.23l-.64-6.4A4 4 0 0 0 17.32 5z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
  'chevron-right': '<path d="m9 18 6-6-6-6"/>',
  'more-horizontal': '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>'
};

function svgWrap(body, size) {
  const px = size || 24;
  return '<svg xmlns="http://www.w3.org/2000/svg" width="' + px + '" height="' + px +
    '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
    body + '</svg>';
}

export function icon(name, size) {
  const body = ICONS[name] || ICONS['alert-circle'];
  return svgWrap(body, size);
}

export function hasIcon(name) {
  return Object.prototype.hasOwnProperty.call(ICONS, name);
}

const CURSOR_SHAPES = {
  arrow: '<path d="M4 2 4 20 9.1 15.1 12.3 22 15.1 20.7 11.9 13.9 19 13.9Z" fill="#fff" stroke="#000" stroke-width="1.6"/>',
  ibeam: '<g stroke="#fff" stroke-width="5" stroke-linecap="round"><path d="M9 4h6M12 4v16M9 20h6"/></g><g stroke="#000" stroke-width="2" stroke-linecap="round"><path d="M9 4h6M12 4v16M9 20h6"/></g>',
  hand: '<path d="M8 12V6a1.5 1.5 0 0 1 3 0v5M11 11V4.5a1.5 1.5 0 0 1 3 0V11M14 11V6a1.5 1.5 0 0 1 3 0v7c0 5-2.5 8-7 8-2 0-3.5-1-5-3l-2.5-4a1.6 1.6 0 0 1 2.6-1.8L8 15z" fill="#fff" stroke="#000" stroke-width="1.6" stroke-linejoin="round"/>',
  wait: '<g stroke="#fff" stroke-width="5" stroke-linejoin="round"><path d="M6 3h12M6 21h12M7 3c0 5 5 6 5 9s-5 4-5 9M17 3c0 5-5 6-5 9s5 4 5 9"/></g><g stroke="#000" stroke-width="2" stroke-linejoin="round"><path d="M6 3h12M6 21h12M7 3c0 5 5 6 5 9s-5 4-5 9M17 3c0 5-5 6-5 9s5 4 5 9"/></g>',
  cross: '<g stroke="#fff" stroke-width="5" stroke-linecap="round"><path d="M12 2v6M12 16v6M2 12h6M16 12h6"/></g><g stroke="#000" stroke-width="2" stroke-linecap="round"><path d="M12 2v6M12 16v6M2 12h6M16 12h6"/></g><circle cx="12" cy="12" r="3" fill="none" stroke="#fff" stroke-width="5"/><circle cx="12" cy="12" r="3" fill="none" stroke="#000" stroke-width="2"/>',
  move: '<g stroke="#fff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v18M3 12h18M9 6l3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3"/></g><g stroke="#000" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v18M3 12h18M9 6l3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3"/></g>',
  no: '<circle cx="12" cy="12" r="9" fill="#fff" stroke="#000" stroke-width="1.6"/><path d="M6 6l12 12" stroke="#000" stroke-width="2.2" stroke-linecap="round"/>'
};

const CURSOR_ALIASES = {
  default: 'arrow',
  pointer: 'hand',
  text: 'ibeam',
  crosshair: 'cross',
  not_allowed: 'no',
  size_ns: 'move',
  size_we: 'move',
  size_nwse: 'move',
  size_nesw: 'move',
  other: 'arrow'
};

/**
 * Cursor artwork with a white core and black edge. The arrow's hotspot is its
 * tip near (4,2) in the 24x24 view box; CSS offsets the element accordingly.
 */
export function cursorIcon(shape) {
  let key = String(shape || 'arrow').toLowerCase();
  if (CURSOR_ALIASES[key]) {
    key = CURSOR_ALIASES[key];
  }
  const body = CURSOR_SHAPES[key] || CURSOR_SHAPES.arrow;
  return '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" ' +
    'fill="none" aria-hidden="true" focusable="false">' + body + '</svg>';
}
