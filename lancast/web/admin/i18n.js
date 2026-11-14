/*
 * Minimal i18n for the LanCast admin page.
 * Chinese is the default; anything else falls back to English.
 * No external lookups, no build step.
 */

const ZH = {
  pageTitle: "LanCast 管理",
  titlePending: "({n}) 待批准 · ",

  connecting: "正在连接…",
  sharing: "共享中",
  paused: "已暂停",
  pauseSharing: "暂停共享",
  resumeSharing: "恢复共享",
  connectionError: "无法连接到 LanCast 服务",
  authError: "管理令牌无效，请使用控制台打印的管理地址打开",

  warningsTitle: "注意",

  pendingTitle: "待批准请求",
  pendingIntro: "新设备正在请求连接，请选择允许观看、允许控制或拒绝。",
  allow: "允许",
  allowWatch: "允许观看",
  allowControl: "允许控制",
  allowControlConfirm: "允许后，{name} 将能操作这台电脑的鼠标和键盘。确定？",
  deny: "拒绝",
  methodQr: "扫码",
  methodPin: "PIN",
  waitingSeconds: "等待 {n} 秒",
  fullUserAgent: "完整 User-Agent",
  allowed: "已允许该设备",
  allowedWatching: "已允许 {name} 观看",
  allowedControlling: "已允许 {name} 观看并控制",
  denied: "已拒绝该设备",

  connectionTitle: "连接设备",
  primaryAddress: "主地址",
  otherAddresses: "其他地址",
  qrUnavailable: "二维码暂不可用",
  qrAlt: "配对二维码",
  pinLabel: "配对码",
  pinRemaining: "剩余有效时间",
  rotatePin: "换一个",
  rotatingPin: "正在更换…",
  pinRotated: "配对码已更换",
  pairingHelp: "手机/平板连同一 Wi-Fi，用相机扫码；或在浏览器输入地址后填写配对码。",
  certHelp: "首次访问会提示证书不受信任，属正常现象，选择继续访问即可。",

  viewersTitle: "正在观看",
  noViewers: "暂无设备连接",
  deviceName: "设备名",
  ip: "IP 地址",
  display: "显示器",
  mode: "模式",
  quality: "画质",
  control: "控制",
  qualitySaver: "省流",
  qualitySmooth: "流畅",
  qualityStandard: "标准",
  qualityHigh: "高清",
  qualityNative: "原画",
  qualityCustom: "自定义",
  controllingBadge: "正在控制",
  remoteControlActive: "远程控制中",
  stopAllControls: "停止所有控制",
  stopAllConfirm: "确定停止所有远程控制吗？所有观看端将立即失去鼠标和键盘操作权限，之后可在设置里重新开启。",
  controlsStopped: "已停止所有远程控制，可在设置里重新开启",
  controlDisabled: "远程控制已关闭",
  controlConfirm: "允许 {name} 控制这台电脑的鼠标和键盘？",
  controlSwitchAria: "允许 {name} 控制",
  controlMasterOffHint: "远程控制总开关已关闭；此设置会记录授权，重新开启总开关后生效。",
  controlAllowed: "已允许该设备控制",
  controlOff: "已关闭该设备的控制权限",
  controlRecordedOff: "已记录授权，但远程控制总开关仍关闭",
  fps: "fps",
  bitrate: "码率 (Mbps)",
  rtt: "RTT (ms)",
  latency: "延迟 (ms)",
  drops: "丢帧",
  connected: "连接时长",
  actions: "操作",
  kick: "断开",
  kicked: "已断开该设备",
  displayN: "显示器 {n}",

  devicesTitle: "已记住的设备",
  noDevices: "暂无已记住的设备",
  lastUsed: "最近使用",
  lastIp: "最近 IP",
  online: "在线",
  offline: "离线",
  revoke: "撤销",
  revokeConfirm: "确定撤销“{name}”的访问权限吗？该设备需要重新配对。",
  revoked: "已撤销设备",
  justNow: "刚刚",
  minutesAgo: "{n} 分钟前",
  hoursAgo: "{n} 小时前",
  daysAgo: "{n} 天前",

  settingsTitle: "共享设置",
  fpsLabel: "帧率",
  resolutionLabel: "最大分辨率",
  resolution720: "720p（1280×720）",
  resolution1080: "1080p（1920×1080）",
  resolution1440: "1440p（2560×1440）",
  resolutionNative: "原始（7680×4320）",
  bitrateLabel: "码率",
  bitrateUnit: "Mbps",
  jpegQuality: "JPEG 质量",
  jpegFps: "JPEG 帧率",
  approval: "新设备需要我批准",
  controlEnabled: "允许远程控制（总开关）",
  viewerQuality: "允许观看端自选画质",
  save: "保存",
  saving: "正在保存…",
  reset: "重置",
  applied: "已应用",
  saveFailed: "保存失败",
  invalidBitrate: "码率必须在 1–200 Mbps 之间。",

  securityTitle: "安全信息",
  tlsFingerprint: "TLS 证书指纹",
  caFingerprint: "本地 CA 指纹",
  downloadCa: "下载 CA 证书",
  securityHelp: "在手机上安装并信任此 CA 后不再有证书警告并可防中间人攻击；只在可信网络上安装；可对照手机上显示的证书指纹。",

  logTitle: "事件日志",
  noLogs: "暂无事件",
  levelInfo: "信息",
  levelWarning: "警告",
  levelError: "错误",
  levelDebug: "调试",

  copy: "复制",
  copyAddress: "复制地址",
  copyFingerprint: "复制指纹",
  copied: "已复制",
  copyFailed: "复制失败，请手动选择文本",

  actionFailed: "操作失败",
  networkError: "无法连接到 LanCast 服务",
  unknownError: "未知错误",

  kindLan: "局域网",
  kindVirtual: "虚拟网卡",
  kindVpn: "VPN",
  kindLoopback: "本机"
};

const EN = {
  pageTitle: "LanCast Admin",
  titlePending: "({n}) Pending · ",

  connecting: "Connecting…",
  sharing: "Sharing",
  paused: "Paused",
  pauseSharing: "Pause sharing",
  resumeSharing: "Resume sharing",
  connectionError: "Cannot reach the LanCast service",
  authError: "Invalid admin token. Open the admin URL printed in the console.",

  warningsTitle: "Warnings",

  pendingTitle: "Pending requests",
  pendingIntro: "A new device is asking to connect. Allow viewing, allow control, or deny it below.",
  allow: "Allow",
  allowWatch: "Allow viewing",
  allowControl: "Allow control",
  allowControlConfirm: "If you allow, {name} will be able to control this computer's mouse and keyboard. Continue?",
  deny: "Deny",
  methodQr: "QR code",
  methodPin: "PIN",
  waitingSeconds: "Waiting {n} s",
  fullUserAgent: "Full User-Agent",
  allowed: "Device allowed",
  allowedWatching: "{name} can now watch",
  allowedControlling: "{name} can now watch and control",
  denied: "Device denied",

  connectionTitle: "Connect a device",
  primaryAddress: "Primary address",
  otherAddresses: "Other addresses",
  qrUnavailable: "QR code unavailable",
  qrAlt: "Pairing QR code",
  pinLabel: "Pairing code",
  pinRemaining: "Time remaining",
  rotatePin: "New code",
  rotatingPin: "Rotating…",
  pinRotated: "Pairing code changed",
  pairingHelp: "Use the same Wi-Fi on your phone or tablet and scan the code with the camera. Or open the address in a browser and enter the pairing code.",
  certHelp: "The first visit will show an untrusted-certificate warning. This is expected; choose to continue.",

  viewersTitle: "Watching now",
  noViewers: "No devices connected",
  deviceName: "Device",
  ip: "IP address",
  display: "Display",
  mode: "Mode",
  quality: "Quality",
  control: "Control",
  qualitySaver: "Saver",
  qualitySmooth: "Smooth",
  qualityStandard: "Standard",
  qualityHigh: "High",
  qualityNative: "Native",
  qualityCustom: "Custom",
  controllingBadge: "Controlling",
  remoteControlActive: "Remote control active",
  stopAllControls: "Stop all control",
  stopAllConfirm: "Stop all remote control? Every viewer will immediately lose mouse and keyboard access. You can turn it back on in settings.",
  controlsStopped: "All remote control stopped. You can turn it back on in settings.",
  controlDisabled: "Remote control off",
  controlConfirm: "Allow {name} to control this computer's mouse and keyboard?",
  controlSwitchAria: "Allow {name} to control",
  controlMasterOffHint: "Remote control is off globally. Permission is still recorded and takes effect when you turn the master switch back on.",
  controlAllowed: "Control allowed for this device",
  controlOff: "Control turned off for this device",
  controlRecordedOff: "Permission saved, but the remote-control master switch is off",
  fps: "fps",
  bitrate: "Bitrate (Mbps)",
  rtt: "RTT (ms)",
  latency: "Latency (ms)",
  drops: "Dropped",
  connected: "Connected",
  actions: "Actions",
  kick: "Disconnect",
  kicked: "Device disconnected",
  displayN: "Display {n}",

  devicesTitle: "Remembered devices",
  noDevices: "No remembered devices",
  lastUsed: "Last used",
  lastIp: "Last IP",
  online: "Online",
  offline: "Offline",
  revoke: "Revoke",
  revokeConfirm: "Revoke access for “{name}”? The device will need to pair again.",
  revoked: "Device revoked",
  justNow: "just now",
  minutesAgo: "{n} min ago",
  hoursAgo: "{n} hr ago",
  daysAgo: "{n} d ago",

  settingsTitle: "Stream settings",
  fpsLabel: "Frame rate",
  resolutionLabel: "Maximum resolution",
  resolution720: "720p (1280×720)",
  resolution1080: "1080p (1920×1080)",
  resolution1440: "1440p (2560×1440)",
  resolutionNative: "Native (7680×4320)",
  bitrateLabel: "Bitrate",
  bitrateUnit: "Mbps",
  jpegQuality: "JPEG quality",
  jpegFps: "JPEG frame rate",
  approval: "New devices need my approval",
  controlEnabled: "Allow remote control (master switch)",
  viewerQuality: "Let viewers choose their own quality",
  save: "Save",
  saving: "Saving…",
  reset: "Reset",
  applied: "Applied",
  saveFailed: "Save failed",
  invalidBitrate: "Bitrate must be between 1 and 200 Mbps.",

  securityTitle: "Security",
  tlsFingerprint: "TLS certificate fingerprint",
  caFingerprint: "Local CA fingerprint",
  downloadCa: "Download CA certificate",
  securityHelp: "After you install and trust this CA on your phone, certificate warnings disappear and man-in-the-middle attacks are prevented. Install it only on a trusted network. You can compare it with the fingerprint shown on the phone.",

  logTitle: "Event log",
  noLogs: "No events",
  levelInfo: "Info",
  levelWarning: "Warning",
  levelError: "Error",
  levelDebug: "Debug",

  copy: "Copy",
  copyAddress: "Copy address",
  copyFingerprint: "Copy fingerprint",
  copied: "Copied",
  copyFailed: "Copy failed; select the text and copy it manually",

  actionFailed: "Action failed",
  networkError: "Cannot reach the LanCast service",
  unknownError: "Unknown error",

  kindLan: "LAN",
  kindVirtual: "Virtual",
  kindVpn: "VPN",
  kindLoopback: "Loopback"
};

export const lang = (navigator.language || "").toLowerCase().startsWith("zh")
  ? "zh"
  : "en";

const TABLE = lang === "zh" ? ZH : EN;

/**
 * Translate a key, interpolating {placeholders}.
 * @param {string} key
 * @param {Record<string, string|number>} [vars]
 * @returns {string}
 */
export function t(key, vars) {
  const template = Object.prototype.hasOwnProperty.call(TABLE, key)
    ? TABLE[key]
    : key;
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (match, name) =>
    Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match
  );
}

/**
 * Translate static markup marked with data-i18n / data-i18n-title /
 * data-i18n-aria-label. Text is always replaced with textContent.
 * @param {ParentNode} [root]
 */
export function applyI18n(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  root.querySelectorAll("[data-i18n-title]").forEach((node) => {
    node.title = t(node.dataset.i18nTitle);
  });
  root.querySelectorAll("[data-i18n-aria-label]").forEach((node) => {
    node.setAttribute("aria-label", t(node.dataset.i18nAriaLabel));
  });
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
}
