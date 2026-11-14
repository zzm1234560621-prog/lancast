/* Inline monochrome line icons (24 px viewBox, stroke 2). */

const PATHS = Object.freeze({
  cast: `
    <path d="M2 8V6a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-6"/>
    <path d="M2 12a9 9 0 0 1 8 8"/>
    <path d="M2 16a5 5 0 0 1 4 4"/>
    <path d="M2 20h.01"/>`,
  pause: `
    <rect x="6" y="4" width="4" height="16" rx="1"/>
    <rect x="14" y="4" width="4" height="16" rx="1"/>`,
  play: `<polygon points="6 3 20 12 6 21 6 3"/>`,
  copy: `
    <rect width="14" height="14" x="8" y="8" rx="2" ry="2"/>
    <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>`,
  check: `<path d="M20 6 9 17l-5-5"/>`,
  "chevron-down": `<path d="m6 9 6 6 6-6"/>`,
  smartphone: `
    <rect width="14" height="20" x="5" y="2" rx="2" ry="2"/>
    <path d="M12 18h.01"/>`,
  "user-check": `
    <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/>
    <circle cx="9" cy="7" r="4"/>
    <polyline points="16 11 18 13 22 9"/>`,
  "user-x": `
    <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/>
    <circle cx="9" cy="7" r="4"/>
    <line x1="17" x2="22" y1="8" y2="13"/>
    <line x1="22" x2="17" y1="8" y2="13"/>`,
  "shield-check": `
    <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>
    <path d="m9 12 2 2 4-4"/>`,
  "shield-alert": `
    <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>
    <path d="M12 8v4"/>
    <path d="M12 16h.01"/>`,
  "sliders-horizontal": `
    <line x1="21" x2="14" y1="4" y2="4"/>
    <line x1="10" x2="3" y1="4" y2="4"/>
    <line x1="21" x2="12" y1="12" y2="12"/>
    <line x1="8" x2="3" y1="12" y2="12"/>
    <line x1="21" x2="16" y1="20" y2="20"/>
    <line x1="12" x2="3" y1="20" y2="20"/>
    <line x1="14" x2="14" y1="2" y2="6"/>
    <line x1="8" x2="8" y1="10" y2="14"/>
    <line x1="16" x2="16" y1="18" y2="22"/>`,
  monitor: `
    <rect width="20" height="14" x="2" y="3" rx="2"/>
    <line x1="8" x2="16" y1="21" y2="21"/>
    <line x1="12" x2="12" y1="17" y2="21"/>`,
  users: `
    <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/>
    <circle cx="9" cy="7" r="4"/>
    <path d="M22 21v-2a4 4 0 0 0-3-3.87"/>
    <path d="M16 3.13a4 4 0 0 1 0 7.75"/>`,
  "scroll-text": `
    <path d="M15 12h-5"/>
    <path d="M15 8h-5"/>
    <path d="M19 17V5a2 2 0 0 0-2-2H4"/>
    <path d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 0-1 1v1a2 2 0 0 0 2 2Z"/>`,
  "triangle-alert": `
    <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/>
    <path d="M12 9v4"/>
    <path d="M12 17h.01"/>`,
  "wifi-off": `
    <path d="M12 20h.01"/>
    <path d="M8.5 16.429a5 5 0 0 1 7 0"/>
    <path d="M5 12.859a10 10 0 0 1 5.17-2.69"/>
    <path d="M19 12.859a10 10 0 0 0-2.007-1.523"/>
    <path d="M2 8.82a15 15 0 0 1 4.177-2.643"/>
    <path d="M22 8.82a15 15 0 0 0-11.288-3.764"/>
    <path d="m2 2 20 20"/>`,
  "rotate-ccw": `
    <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>
    <path d="M3 3v5h5"/>`,
  "refresh-cw": `
    <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/>
    <path d="M21 3v5h-5"/>
    <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/>
    <path d="M8 16H3v5"/>`,
  download: `
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
    <polyline points="7 10 12 15 17 10"/>
    <line x1="12" x2="12" y1="15" y2="3"/>`,
  clock: `
    <circle cx="12" cy="12" r="10"/>
    <polyline points="12 6 12 12 16 14"/>`,
  x: `
    <path d="M18 6 6 18"/>
    <path d="m6 6 12 12"/>`,
  unplug: `
    <path d="m19 5 3-3"/>
    <path d="m2 22 3-3"/>
    <path d="M6.3 20.3a2.4 2.4 0 0 0 3.4 0L12 18l-6-6-2.3 2.3a2.4 2.4 0 0 0 0 3.4Z"/>
    <path d="M7.5 13.5 10 11"/>
    <path d="M10.5 16.5 13 14"/>
    <path d="m12 6 6 6 2.3-2.3a2.4 2.4 0 0 0 0-3.4l-2.6-2.6a2.4 2.4 0 0 0-3.4 0Z"/>`,
  key: `
    <path d="M2.586 17.414A2 2 0 0 0 2 18.828V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.172a2 2 0 0 0 1.414-.586l.814-.814a6.5 6.5 0 1 0-4-4z"/>
    <circle cx="16.5" cy="7.5" r=".5"/>`,
  eye: `
    <path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/>
    <circle cx="12" cy="12" r="3"/>`,
  "mouse-pointer": `
    <path d="M12.586 12.586 19 19"/>
    <path d="M3.688 3.037a.497.497 0 0 0-.651.651l6.5 15.999a.501.501 0 0 0 .947-.062l1.569-6.083a2 2 0 0 1 1.448-1.479l6.124-1.579a.5.5 0 0 0 .063-.947z"/>`,
  "circle-stop": `
    <circle cx="12" cy="12" r="10"/>
    <rect x="9" y="9" width="6" height="6" rx="1"/>`
});

/**
 * @param {string} name
 * @param {string} [className]
 * @returns {string} SVG markup (never contains caller-provided text)
 */
export function icon(name, className = "icon") {
  const body = PATHS[name] || PATHS["shield-alert"];
  return (
    `<svg class="${className}" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
    `stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ` +
    `aria-hidden="true" focusable="false">${body}</svg>`
  );
}

/**
 * Create a span wrapping an inline icon, for DOM construction.
 * @param {string} name
 * @returns {HTMLSpanElement}
 */
export function makeIcon(name) {
  const span = document.createElement("span");
  span.className = "icon-slot";
  span.innerHTML = icon(name);
  return span;
}

/**
 * Replace empty [data-icon] placeholders with inline SVGs.
 * @param {ParentNode} [root]
 */
export function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((node) => {
    const name = node.dataset.icon;
    if (name && !node.firstChild) {
      node.innerHTML = icon(name);
    }
  });
}
