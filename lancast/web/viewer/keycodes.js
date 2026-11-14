/*
 * Keyboard / mouse target table for the remote-control UI.
 *
 * The key codes below are exactly the names listed in docs/PROTOCOL.md §3.7.
 * "Targets" additionally include the mouse buttons and wheel directions that
 * key-mapping buttons may send; those are translated to mb / mw events.
 *
 * No DOM access: this module is imported by selftest.mjs in Node.
 */

function range(prefix, from, to) {
  const out = [];
  for (let i = from; i <= to; i += 1) {
    out.push(prefix + i);
  }
  return out;
}

function letters() {
  const out = [];
  for (let i = 0; i < 26; i += 1) {
    out.push('Key' + String.fromCharCode(65 + i));
  }
  return out;
}

export const KEY_GROUP_DEFS = [
  { id: 'letters', labelKey: 'keys.group.letters', codes: letters() },
  { id: 'digits', labelKey: 'keys.group.digits', codes: range('Digit', 0, 9) },
  { id: 'function', labelKey: 'keys.group.function', codes: range('F', 1, 24) },
  {
    id: 'navigation',
    labelKey: 'keys.group.navigation',
    codes: [
      'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
      'Home', 'End', 'PageUp', 'PageDown', 'Insert', 'Delete',
      'Backspace', 'Tab', 'Enter', 'Space', 'Escape',
      'CapsLock', 'NumLock', 'ScrollLock', 'PrintScreen', 'Pause', 'ContextMenu'
    ]
  },
  {
    id: 'modifiers',
    labelKey: 'keys.group.modifiers',
    codes: [
      'ShiftLeft', 'ShiftRight', 'ControlLeft', 'ControlRight',
      'AltLeft', 'AltRight', 'MetaLeft', 'MetaRight'
    ]
  },
  {
    id: 'symbols',
    labelKey: 'keys.group.symbols',
    codes: [
      'Minus', 'Equal', 'BracketLeft', 'BracketRight', 'Backslash',
      'Semicolon', 'Quote', 'Backquote', 'Comma', 'Period', 'Slash',
      'IntlBackslash'
    ]
  },
  {
    id: 'numpad',
    labelKey: 'keys.group.numpad',
    codes: range('Numpad', 0, 9).concat([
      'NumpadAdd', 'NumpadSubtract', 'NumpadMultiply', 'NumpadDivide',
      'NumpadDecimal', 'NumpadEnter'
    ])
  },
  {
    id: 'media',
    labelKey: 'keys.group.media',
    codes: [
      'MediaPlayPause', 'MediaTrackNext', 'MediaTrackPrevious', 'MediaStop',
      'AudioVolumeUp', 'AudioVolumeDown', 'AudioVolumeMute',
      'BrowserBack', 'BrowserForward', 'BrowserRefresh'
    ]
  },
  {
    id: 'mouse',
    labelKey: 'keys.group.mouse',
    codes: [
      'MouseLeft', 'MouseRight', 'MouseMiddle', 'MouseX1', 'MouseX2',
      'WheelUp', 'WheelDown'
    ]
  }
];

/** All KeyboardEvent.code values accepted by the server (127 entries). */
export const KEY_CODES = KEY_GROUP_DEFS.reduce(function (all, group) {
  return group.id === 'mouse' ? all : all.concat(group.codes);
}, []);

export const MOUSE_BUTTON_TARGETS = ['MouseLeft', 'MouseRight', 'MouseMiddle', 'MouseX1', 'MouseX2'];
export const WHEEL_TARGETS = ['WheelUp', 'WheelDown'];
export const MOUSE_TARGETS = MOUSE_BUTTON_TARGETS.concat(WHEEL_TARGETS);

/** Everything a layout button may send: key codes + mouse/wheel targets. */
export const INPUT_TARGETS = KEY_CODES.concat(MOUSE_TARGETS);

const KEY_CODE_SET = new Set(KEY_CODES);
const INPUT_TARGET_SET = new Set(INPUT_TARGETS);
const MOUSE_BUTTON_SET = new Set(MOUSE_BUTTON_TARGETS);
const WHEEL_TARGET_SET = new Set(WHEEL_TARGETS);

const MOUSE_BUTTON_NAMES = {
  MouseLeft: 'left',
  MouseRight: 'right',
  MouseMiddle: 'middle',
  MouseX1: 'x1',
  MouseX2: 'x2'
};

const LABELS = {
  ArrowUp: '\u2191', ArrowDown: '\u2193', ArrowLeft: '\u2190', ArrowRight: '\u2192',
  Home: 'Home', End: 'End', PageUp: 'PgUp', PageDown: 'PgDn',
  Insert: 'Ins', Delete: 'Del', Backspace: 'Backspace', Tab: 'Tab',
  Enter: 'Enter', Space: 'Space', Escape: 'Esc', CapsLock: 'Caps',
  NumLock: 'Num', ScrollLock: 'ScrLk', PrintScreen: 'PrtSc', Pause: 'Pause',
  ContextMenu: 'Menu',
  ShiftLeft: 'L Shift', ShiftRight: 'R Shift',
  ControlLeft: 'L Ctrl', ControlRight: 'R Ctrl',
  AltLeft: 'L Alt', AltRight: 'R Alt',
  MetaLeft: 'L Meta', MetaRight: 'R Meta',
  Minus: '-', Equal: '=', BracketLeft: '[', BracketRight: ']', Backslash: '\\',
  Semicolon: ';', Quote: "'", Backquote: '`', Comma: ',', Period: '.',
  Slash: '/', IntlBackslash: 'Intl \\',
  NumpadAdd: 'Num +', NumpadSubtract: 'Num -', NumpadMultiply: 'Num *',
  NumpadDivide: 'Num /', NumpadDecimal: 'Num .', NumpadEnter: 'Num Enter',
  MediaPlayPause: 'Play/Pause', MediaTrackNext: 'Next Track',
  MediaTrackPrevious: 'Prev Track', MediaStop: 'Stop',
  AudioVolumeUp: 'Vol +', AudioVolumeDown: 'Vol -', AudioVolumeMute: 'Mute',
  BrowserBack: 'Browser Back', BrowserForward: 'Browser Fwd',
  BrowserRefresh: 'Refresh',
  MouseLeft: 'Mouse L', MouseRight: 'Mouse R', MouseMiddle: 'Mouse M',
  MouseX1: 'Mouse X1', MouseX2: 'Mouse X2',
  WheelUp: 'Wheel \u2191', WheelDown: 'Wheel \u2193'
};

export function isKeyCode(code) {
  return KEY_CODE_SET.has(code);
}

export function isInputTarget(target) {
  return INPUT_TARGET_SET.has(target);
}

export function isMouseButtonTarget(target) {
  return MOUSE_BUTTON_SET.has(target);
}

export function isWheelTarget(target) {
  return WHEEL_TARGET_SET.has(target);
}

/** Group id for a target ('letters' … 'mouse'). */
export function groupOf(target) {
  for (let i = 0; i < KEY_GROUP_DEFS.length; i += 1) {
    if (KEY_GROUP_DEFS[i].codes.indexOf(target) >= 0) {
      return KEY_GROUP_DEFS[i].id;
    }
  }
  return null;
}

/** Short display label. i18n may override mouse targets with translated text. */
export function keyLabel(code) {
  if (LABELS[code]) {
    return LABELS[code];
  }
  if (/^Key[A-Z]$/.test(code)) {
    return code.slice(3);
  }
  if (/^Digit[0-9]$/.test(code)) {
    return code.slice(5);
  }
  if (/^Numpad[0-9]$/.test(code)) {
    return 'Num ' + code.slice(6);
  }
  return String(code || '');
}

/** PROTOCOL mouse button name for a MouseXxx target, or null. */
export function mouseButtonFor(target) {
  return MOUSE_BUTTON_NAMES[target] || null;
}

/** Wheel delta for WheelUp/WheelDown (120 = one notch), otherwise 0. */
export function wheelDeltaFor(target) {
  if (target === 'WheelUp') {
    return 120;
  }
  if (target === 'WheelDown') {
    return -120;
  }
  return 0;
}

/**
 * Translate one mapping target press/release into a protocol event.
 * Returns null when the target does nothing for that edge (wheel on release).
 */
export function targetEvent(target, down) {
  if (isKeyCode(target)) {
    return { t: down ? 'kd' : 'ku', k: target };
  }
  if (isMouseButtonTarget(target)) {
    return { t: 'mb', b: mouseButtonFor(target), d: !!down };
  }
  if (isWheelTarget(target)) {
    if (!down) {
      return null;
    }
    return { t: 'mw', dy: wheelDeltaFor(target), dx: 0 };
  }
  return null;
}
