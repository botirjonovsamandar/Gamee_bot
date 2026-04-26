// gamee_bot/js_preload.js
// DOM / Browser environment polyfill for headless V8 sandbox.
// Injected before any challenge or integrity script execution.
// Mimics a Telegram Android WebView on a mobile device.

var _global = (typeof globalThis !== "undefined") ? globalThis : (typeof self !== "undefined") ? self : this;

(function(_g) {

// ── Time ──────────────────────────────────────────────────────────────
var _startTime = Date.now();
var _perfOrigin = _startTime - Math.floor(Math.random() * 200 + 50);

if (typeof performance === "undefined") {
  var performance = {};
}
performance.now = performance.now || function() {
  return Date.now() - _perfOrigin;
};
performance.timeOrigin = performance.timeOrigin || _perfOrigin;
performance.timing = performance.timing || {
  navigationStart: _perfOrigin,
  fetchStart: _perfOrigin + 1,
  domContentLoadedEventEnd: _perfOrigin + 800 + Math.floor(Math.random() * 400),
  loadEventEnd: _perfOrigin + 1200 + Math.floor(Math.random() * 600),
};
performance.getEntriesByType = performance.getEntriesByType || function() { return []; };
performance.getEntriesByName = performance.getEntriesByName || function() { return []; };
performance.mark = performance.mark || function() {};
performance.measure = performance.measure || function() {};

// ── Console ───────────────────────────────────────────────────────────
if (typeof console === "undefined") {
  var console = {};
}
["log","warn","error","info","debug","trace","dir","table","group","groupEnd","time","timeEnd"].forEach(function(m) {
  if (!console[m]) console[m] = function() {};
});

// ── Crypto ────────────────────────────────────────────────────────────
if (typeof crypto === "undefined") {
  var crypto = {};
}
crypto.getRandomValues = crypto.getRandomValues || function(arr) {
  for (var i = 0; i < arr.length; i++) {
    arr[i] = Math.floor(Math.random() * 256);
  }
  return arr;
};
crypto.randomUUID = crypto.randomUUID || function() {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function(c) {
    var r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
};
crypto.subtle = crypto.subtle || {
  digest: function() { return Promise.resolve(new ArrayBuffer(32)); },
  importKey: function() { return Promise.resolve({}); },
  sign: function() { return Promise.resolve(new ArrayBuffer(32)); },
  encrypt: function() { return Promise.resolve(new ArrayBuffer(0)); },
  decrypt: function() { return Promise.resolve(new ArrayBuffer(0)); },
};

// ── setTimeout / setInterval stubs ────────────────────────────────────
if (typeof setTimeout === "undefined") {
  function setTimeout(fn, ms) {
    if (typeof fn === "function") fn();
    return 0;
  }
}
if (typeof setInterval === "undefined") {
  function setInterval() { return 0; }
}
if (typeof clearTimeout === "undefined") {
  function clearTimeout() {}
}
if (typeof clearInterval === "undefined") {
  function clearInterval() {}
}
if (typeof requestAnimationFrame === "undefined") {
  function requestAnimationFrame(fn) { if (typeof fn === "function") fn(performance.now()); return 0; }
}
if (typeof cancelAnimationFrame === "undefined") {
  function cancelAnimationFrame() {}
}

// ── Navigator ─────────────────────────────────────────────────────────
// __SANDBOX_USER_AGENT__ is replaced by Python before injection.
var _ua = (typeof __SANDBOX_USER_AGENT__ !== "undefined") ? __SANDBOX_USER_AGENT__ : "Mozilla/5.0 (Linux; Android 14; K) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/131.0.0.0 Mobile Safari/537.36";

var navigator = {
  userAgent: _ua,
  appVersion: _ua.replace("Mozilla/", ""),
  platform: "Linux armv8l",
  vendor: "Google Inc.",
  language: "en-US",
  languages: ["en-US", "en"],
  onLine: true,
  cookieEnabled: true,
  doNotTrack: null,
  maxTouchPoints: 5,
  hardwareConcurrency: 8,
  deviceMemory: 8,
  pdfViewerEnabled: false,
  // Crucial: webdriver must NOT be true (bot detection signal)
  webdriver: false,
  connection: {
    effectiveType: "4g",
    downlink: 10,
    rtt: 50,
    saveData: false,
  },
  mediaDevices: {
    enumerateDevices: function() { return Promise.resolve([]); },
    getUserMedia: function() { return Promise.reject(new Error("Not allowed")); },
  },
  permissions: {
    query: function() { return Promise.resolve({ state: "prompt" }); },
  },
  clipboard: {
    writeText: function() { return Promise.resolve(); },
    readText: function() { return Promise.resolve(""); },
  },
  getBattery: function() {
    return Promise.resolve({
      charging: true,
      chargingTime: Infinity,
      dischargingTime: Infinity,
      level: 0.87 + Math.random() * 0.12,
    });
  },
  getGamepads: function() { return []; },
  vibrate: function() { return true; },
  sendBeacon: function() { return true; },
  javaEnabled: function() { return false; },
};

// ── Screen ────────────────────────────────────────────────────────────
var screen = {
  width: 393,
  height: 852,
  availWidth: 393,
  availHeight: 825,
  colorDepth: 24,
  pixelDepth: 24,
  orientation: {
    type: "portrait-primary",
    angle: 0,
  },
};

// ── Window ────────────────────────────────────────────────────────────
if (typeof window === "undefined") {
  var window = this || {};
}
window.innerWidth = 393;
window.innerHeight = 852;
window.outerWidth = 393;
window.outerHeight = 852;
window.devicePixelRatio = 2.75;
window.scrollX = 0;
window.scrollY = 0;
window.pageXOffset = 0;
window.pageYOffset = 0;
window.navigator = navigator;
window.screen = screen;
window.performance = performance;
window.crypto = crypto;
window.console = console;
window.location = {
  href: "https://prizes.gamee.com/",
  origin: "https://prizes.gamee.com",
  protocol: "https:",
  host: "prizes.gamee.com",
  hostname: "prizes.gamee.com",
  port: "",
  pathname: "/",
  search: "",
  hash: "",
  assign: function() {},
  replace: function() {},
  reload: function() {},
  toString: function() { return this.href; },
};
window.origin = "https://prizes.gamee.com";
window.isSecureContext = true;
window.crossOriginIsolated = false;
window.self = window;
window.top = window;
window.parent = window;
window.frames = window;
window.frameElement = null;
window.length = 0;
window.name = "";
window.closed = false;
window.opener = null;
window.visualViewport = {
  width: 393,
  height: 852,
  offsetLeft: 0,
  offsetTop: 0,
  pageLeft: 0,
  pageTop: 0,
  scale: 1,
};

// Event system stubs
window.addEventListener = function() {};
window.removeEventListener = function() {};
window.dispatchEvent = function() { return true; };
window.postMessage = function() {};
window.close = function() {};
window.focus = function() {};
window.blur = function() {};
window.open = function() { return null; };
window.print = function() {};
window.stop = function() {};
window.alert = function() {};
window.confirm = function() { return true; };
window.prompt = function() { return null; };
window.getComputedStyle = function() {
  return new Proxy({}, { get: function() { return ""; } });
};
window.matchMedia = function(q) {
  return {
    matches: false,
    media: q,
    addEventListener: function() {},
    removeEventListener: function() {},
  };
};
window.requestIdleCallback = function(fn) { fn({ didTimeout: false, timeRemaining: function() { return 50; } }); return 0; };
window.cancelIdleCallback = function() {};
window.btoa = function(s) {
  // Basic btoa for ASCII
  var chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  var out = "";
  for (var i = 0; i < s.length; i += 3) {
    var a = s.charCodeAt(i), b = s.charCodeAt(i+1), c = s.charCodeAt(i+2);
    var n = (a << 16) | ((b || 0) << 8) | (c || 0);
    out += chars[(n >> 18) & 63] + chars[(n >> 12) & 63];
    out += (i+1 < s.length) ? chars[(n >> 6) & 63] : "=";
    out += (i+2 < s.length) ? chars[n & 63] : "=";
  }
  return out;
};
window.atob = function(s) {
  var chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=";
  var out = "";
  s = s.replace(/=+$/, "");
  for (var i = 0; i < s.length; i += 4) {
    var a = chars.indexOf(s[i]), b = chars.indexOf(s[i+1]);
    var c = chars.indexOf(s[i+2]), d = chars.indexOf(s[i+3]);
    var n = (a << 18) | (b << 12) | ((c >= 0 ? c : 0) << 6) | (d >= 0 ? d : 0);
    out += String.fromCharCode((n >> 16) & 255);
    if (c >= 0) out += String.fromCharCode((n >> 8) & 255);
    if (d >= 0) out += String.fromCharCode(n & 255);
  }
  return out;
};

// ── Document ──────────────────────────────────────────────────────────
var _elements = {};
function _makeElement(tag) {
  var el = {
    tagName: (tag || "DIV").toUpperCase(),
    id: "",
    className: "",
    innerHTML: "",
    textContent: "",
    style: new Proxy({}, { get: function() { return ""; }, set: function() { return true; } }),
    dataset: {},
    attributes: {},
    children: [],
    childNodes: [],
    parentNode: null,
    parentElement: null,
    nextSibling: null,
    previousSibling: null,
    firstChild: null,
    lastChild: null,
    ownerDocument: null,
    nodeType: 1,
    nodeName: (tag || "DIV").toUpperCase(),
    getAttribute: function(n) { return this.attributes[n] || null; },
    setAttribute: function(n, v) { this.attributes[n] = v; },
    removeAttribute: function(n) { delete this.attributes[n]; },
    hasAttribute: function(n) { return n in this.attributes; },
    appendChild: function(c) { this.children.push(c); this.childNodes.push(c); c.parentNode = this; return c; },
    removeChild: function(c) { var i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; },
    insertBefore: function(n, r) { this.children.push(n); return n; },
    cloneNode: function() { return _makeElement(this.tagName); },
    addEventListener: function() {},
    removeEventListener: function() {},
    dispatchEvent: function() { return true; },
    querySelector: function() { return null; },
    querySelectorAll: function() { return []; },
    getElementsByClassName: function() { return []; },
    getElementsByTagName: function() { return []; },
    getBoundingClientRect: function() { return { top: 0, left: 0, bottom: 0, right: 0, width: 0, height: 0, x: 0, y: 0 }; },
    contains: function() { return false; },
    matches: function() { return false; },
    closest: function() { return null; },
    focus: function() {},
    blur: function() {},
    click: function() {},
    remove: function() {},
  };
  return el;
}

var _head = _makeElement("HEAD");
var _body = _makeElement("BODY");
var _docEl = _makeElement("HTML");
_docEl.children = [_head, _body];

var document = {
  documentElement: _docEl,
  head: _head,
  body: _body,
  title: "Gamee",
  readyState: "complete",
  visibilityState: "visible",
  hidden: false,
  domain: "prizes.gamee.com",
  URL: "https://prizes.gamee.com/",
  referrer: "",
  cookie: "",
  characterSet: "UTF-8",
  contentType: "text/html",
  compatMode: "CSS1Compat",

  createElement: function(tag) {
    var el = _makeElement(tag);
    var lt = String(tag || "").toLowerCase();
    // Canvas: дать ему getContext с реальным WebGL/2D stub (как HTMLCanvasElement.prototype)
    if (lt === "canvas") {
      try {
        if (window.HTMLCanvasElement && window.HTMLCanvasElement.prototype) {
          var proto = window.HTMLCanvasElement.prototype;
          el.getContext = proto.getContext || function() { return null; };
          el.toDataURL = proto.toDataURL || function() { return "data:image/png;base64,"; };
          el.toBlob = proto.toBlob || function(cb) { try { cb(null); } catch(e){} };
          el.width = el.width || 300;
          el.height = el.height || 150;
        }
      } catch (e) {}
    }
    return el;
  },
  createElementNS: function(ns, tag) { return _makeElement(tag); },
  createTextNode: function(t) { return { nodeType: 3, textContent: t, data: t }; },
  createDocumentFragment: function() {
    var f = _makeElement("fragment");
    f.nodeType = 11;
    return f;
  },
  createComment: function(t) { return { nodeType: 8, textContent: t }; },
  createEvent: function() {
    return { initEvent: function() {}, preventDefault: function() {}, stopPropagation: function() {} };
  },

  getElementById: function(id) { return _elements[id] || null; },
  getElementsByClassName: function() { return []; },
  getElementsByTagName: function(tag) {
    tag = tag.toUpperCase();
    if (tag === "HEAD") return [_head];
    if (tag === "BODY") return [_body];
    if (tag === "HTML") return [_docEl];
    return [];
  },
  querySelector: function(sel) {
    if (sel === "head") return _head;
    if (sel === "body") return _body;
    if (sel === "html") return _docEl;
    return null;
  },
  querySelectorAll: function() { return []; },

  addEventListener: function() {},
  removeEventListener: function() {},
  dispatchEvent: function() { return true; },

  hasFocus: function() { return true; },
  getSelection: function() { return { rangeCount: 0, toString: function() { return ""; } }; },
  exitFullscreen: function() { return Promise.resolve(); },
};

window.document = document;

// ── Storage stubs ─────────────────────────────────────────────────────
function _makeStorage() {
  var data = {};
  return {
    getItem: function(k) { return data.hasOwnProperty(k) ? data[k] : null; },
    setItem: function(k, v) { data[k] = String(v); },
    removeItem: function(k) { delete data[k]; },
    clear: function() { data = {}; },
    get length() { return Object.keys(data).length; },
    key: function(i) { var keys = Object.keys(data); return i < keys.length ? keys[i] : null; },
  };
}
window.localStorage = _makeStorage();
window.sessionStorage = _makeStorage();

// ── Fetch stubs ───────────────────────────────────────────────────────
window.fetch = function() {
  return Promise.resolve({
    ok: true, status: 200, statusText: "OK",
    json: function() { return Promise.resolve({}); },
    text: function() { return Promise.resolve(""); },
    arrayBuffer: function() { return Promise.resolve(new ArrayBuffer(0)); },
    headers: { get: function() { return null; } },
  });
};
window.XMLHttpRequest = function() {
  this.open = function() {};
  this.send = function() {};
  this.setRequestHeader = function() {};
  this.addEventListener = function() {};
  this.readyState = 4;
  this.status = 200;
  this.responseText = "";
};

// ── Canvas fingerprint stub ───────────────────────────────────────────
window.HTMLCanvasElement = function() {};
window.HTMLCanvasElement.prototype = {
  getContext: function(type) {
    if (type === "2d") {
      return {
        fillRect: function() {},
        fillText: function() {},
        measureText: function() { return { width: 10 }; },
        getImageData: function(x, y, w, h) {
          var data = new Uint8ClampedArray(w * h * 4);
          for (var i = 0; i < data.length; i += 4) {
            data[i] = 200 + Math.floor(Math.random() * 50);
            data[i+1] = 200 + Math.floor(Math.random() * 50);
            data[i+2] = 200 + Math.floor(Math.random() * 50);
            data[i+3] = 255;
          }
          return { data: data, width: w, height: h };
        },
        canvas: { toDataURL: function() { return "data:image/png;base64,iVBOR"; } },
        beginPath: function() {},
        closePath: function() {},
        arc: function() {},
        fill: function() {},
        stroke: function() {},
        moveTo: function() {},
        lineTo: function() {},
        rect: function() {},
        clearRect: function() {},
        drawImage: function() {},
        createLinearGradient: function() { return { addColorStop: function() {} }; },
        createRadialGradient: function() { return { addColorStop: function() {} }; },
        createPattern: function() { return {}; },
        save: function() {},
        restore: function() {},
        translate: function() {},
        rotate: function() {},
        scale: function() {},
        transform: function() {},
        setTransform: function() {},
        globalAlpha: 1,
        globalCompositeOperation: "source-over",
        fillStyle: "#000",
        strokeStyle: "#000",
        lineWidth: 1,
        font: "10px sans-serif",
        textAlign: "start",
        textBaseline: "alphabetic",
      };
    }
    if (type === "webgl" || type === "webgl2" || type === "experimental-webgl") {
      return {
        getParameter: function(p) {
          // RENDERER / VENDOR for WebGL fingerprint (per-account from __SANDBOX_WEBGL_PARAMS__)
          var params = window.__SANDBOX_WEBGL_PARAMS__ || {};
          if (p === 0x1F01) return params["RENDERER"] || "Adreno (TM) 730";
          if (p === 0x1F00) return params["VENDOR"] || "Qualcomm";
          if (p === 0x9246) return params[37446] || params["RENDERER"] || "Adreno (TM) 730";
          if (p === 0x9245) return params[37445] || params["VENDOR"] || "Qualcomm";
          return null;
        },
        getExtension: function(name) {
          if (name === "WEBGL_debug_renderer_info") return { UNMASKED_VENDOR_WEBGL: 0x9245, UNMASKED_RENDERER_WEBGL: 0x9246 };
          return null;
        },
        getSupportedExtensions: function() { return ["WEBGL_debug_renderer_info"]; },
        createBuffer: function() { return {}; },
        bindBuffer: function() {},
        bufferData: function() {},
        createProgram: function() { return {}; },
        createShader: function() { return {}; },
        shaderSource: function() {},
        compileShader: function() {},
        attachShader: function() {},
        linkProgram: function() {},
        useProgram: function() {},
        drawArrays: function() {},
        canvas: { toDataURL: function() { return "data:image/png;base64,iVBOR"; } },
      };
    }
    return null;
  },
  toDataURL: function() { return "data:image/png;base64,iVBOR"; },
  toBlob: function(cb) { cb(new Blob()); },
  width: 300,
  height: 150,
};

// ── WebGL prototype (for feature checks) ──────────────────────────────
window.WebGLRenderingContext = function() {};
window.WebGL2RenderingContext = function() {};

// ── AudioContext stub (fingerprint) ───────────────────────────────────
window.AudioContext = window.AudioContext || function() {
  this.state = "running";
  this.sampleRate = 48000;
  this.currentTime = 0;
  this.destination = {};
  this.createOscillator = function() {
    return {
      type: "sine", frequency: { value: 440 },
      connect: function() {}, start: function() {}, stop: function() {},
    };
  };
  this.createDynamicsCompressor = function() {
    return {
      threshold: { value: -50 }, knee: { value: 40 },
      ratio: { value: 12 }, reduction: { value: 0 },
      attack: { value: 0.003 }, release: { value: 0.25 },
      connect: function() {},
    };
  };
  this.createAnalyser = function() {
    return {
      fftSize: 2048, connect: function() {},
      getFloatFrequencyData: function(a) { for (var i=0;i<a.length;i++) a[i] = -100 + Math.random()*80; },
    };
  };
  this.createGain = function() { return { gain: { value: 1 }, connect: function() {} }; };
  this.close = function() { return Promise.resolve(); };
};
window.webkitAudioContext = window.AudioContext;
window.OfflineAudioContext = window.AudioContext;

// ── Error/Event constructors ──────────────────────────────────────────
if (typeof Event === "undefined") {
  function Event(type) { this.type = type; this.bubbles = false; this.cancelable = false; }
}
if (typeof CustomEvent === "undefined") {
  function CustomEvent(type, params) { this.type = type; this.detail = (params || {}).detail || null; }
}
if (typeof ErrorEvent === "undefined") {
  function ErrorEvent(type) { this.type = type; }
}
if (typeof MessageEvent === "undefined") {
  function MessageEvent(type, init) { this.type = type; this.data = (init || {}).data || null; this.origin = (init || {}).origin || ""; }
}

// ── Blob / URL stubs ──────────────────────────────────────────────────
if (typeof Blob === "undefined") {
  function Blob(parts, opts) { this.size = 0; this.type = (opts || {}).type || ""; }
}
window.URL = window.URL || {
  createObjectURL: function() { return "blob:null/00000000-0000-0000-0000-000000000000"; },
  revokeObjectURL: function() {},
};

// ── MutationObserver / ResizeObserver / IntersectionObserver ───────────
window.MutationObserver = function() { this.observe = function() {}; this.disconnect = function() {}; this.takeRecords = function() { return []; }; };
window.ResizeObserver = function() { this.observe = function() {}; this.unobserve = function() {}; this.disconnect = function() {}; };
window.IntersectionObserver = function() { this.observe = function() {}; this.unobserve = function() {}; this.disconnect = function() {}; };

// ── History / Location stubs ──────────────────────────────────────────
window.history = { length: 1, state: null, pushState: function() {}, replaceState: function() {}, back: function() {}, forward: function() {}, go: function() {} };

// ── Telegram WebApp placeholder ───────────────────────────────────────
// This is a minimal placeholder. The real synthesis is done by
// _inject_telegram_webapp() in js_runtime.py which replaces this block
// with a fully populated object containing actual initData.
window.Telegram = window.Telegram || {};
window.Telegram.WebApp = window.Telegram.WebApp || {
  initData: "",
  initDataUnsafe: {},
  version: "8.0",
  platform: "android",
  colorScheme: "dark",
  themeParams: {
    bg_color: "#212121",
    text_color: "#ffffff",
    hint_color: "#aaaaaa",
    link_color: "#8774e1",
    button_color: "#8774e1",
    button_text_color: "#ffffff",
    secondary_bg_color: "#181818",
    header_bg_color: "#212121",
    accent_text_color: "#8774e1",
    section_bg_color: "#212121",
    section_header_text_color: "#8774e1",
    subtitle_text_color: "#aaaaaa",
    destructive_text_color: "#ee686f",
  },
  isExpanded: true,
  viewportHeight: 852,
  viewportStableHeight: 852,
  headerColor: "#212121",
  backgroundColor: "#212121",
  isClosingConfirmationEnabled: false,
  BackButton: { isVisible: false, onClick: function() {}, offClick: function() {}, show: function() {}, hide: function() {} },
  MainButton: { text: "", color: "", textColor: "", isVisible: false, isActive: true, isProgressVisible: false, setText: function() {}, onClick: function() {}, offClick: function() {}, show: function() {}, hide: function() {}, enable: function() {}, disable: function() {}, showProgress: function() {}, hideProgress: function() {} },
  HapticFeedback: { impactOccurred: function() {}, notificationOccurred: function() {}, selectionChanged: function() {} },
  isVersionAtLeast: function(v) { return parseFloat("8.0") >= parseFloat(v); },
  setHeaderColor: function() {},
  setBackgroundColor: function() {},
  enableClosingConfirmation: function() {},
  disableClosingConfirmation: function() {},
  onEvent: function() {},
  offEvent: function() {},
  sendData: function() {},
  switchInlineQuery: function() {},
  openLink: function() {},
  openTelegramLink: function() {},
  openInvoice: function() {},
  showPopup: function() {},
  showAlert: function() {},
  showConfirm: function() {},
  ready: function() {},
  expand: function() {},
  close: function() {},
};

// ── Expose globals on the global scope ────────────────────────────────
_g.window = window;
_g.self = window;
_g.document = document;
_g.navigator = navigator;
_g.screen = screen;
_g.performance = performance;
_g.console = console;
_g.crypto = crypto;
_g.localStorage = window.localStorage;
_g.sessionStorage = window.sessionStorage;
_g.fetch = window.fetch;
_g.XMLHttpRequest = window.XMLHttpRequest;
_g.setTimeout = typeof setTimeout !== "undefined" ? setTimeout : function(fn) { if (typeof fn === "function") fn(); return 0; };
_g.setInterval = typeof setInterval !== "undefined" ? setInterval : function() { return 0; };
_g.clearTimeout = typeof clearTimeout !== "undefined" ? clearTimeout : function() {};
_g.clearInterval = typeof clearInterval !== "undefined" ? clearInterval : function() {};
_g.requestAnimationFrame = typeof requestAnimationFrame !== "undefined" ? requestAnimationFrame : function(fn) { fn(performance.now()); return 0; };
_g.cancelAnimationFrame = typeof cancelAnimationFrame !== "undefined" ? cancelAnimationFrame : function() {};
_g.Blob = typeof Blob !== "undefined" ? Blob : function() {};
_g.Event = typeof Event !== "undefined" ? Event : function(t) { this.type = t; };
_g.CustomEvent = typeof CustomEvent !== "undefined" ? CustomEvent : function(t) { this.type = t; };
_g.MutationObserver = window.MutationObserver;
_g.ResizeObserver = window.ResizeObserver;
_g.IntersectionObserver = window.IntersectionObserver;
_g.AudioContext = window.AudioContext;
_g.webkitAudioContext = window.AudioContext;
_g.HTMLCanvasElement = window.HTMLCanvasElement;
_g.WebGLRenderingContext = window.WebGLRenderingContext;
_g.WebGL2RenderingContext = window.WebGL2RenderingContext;
_g.Telegram = window.Telegram;

// ── Phase 15: Extended browser API stubs ─────────────────────────────

// JS-3: WebRTC stubs (RTCPeerConnection)
window.RTCPeerConnection = window.RTCPeerConnection || function() {
  this.localDescription = null;
  this.remoteDescription = null;
  this.signalingState = "stable";
  this.connectionState = "new";
  this.iceConnectionState = "new";
  this.iceGatheringState = "new";
  this.createOffer = function() { return Promise.resolve({ type: "offer", sdp: "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n" }); };
  this.createAnswer = function() { return Promise.resolve({ type: "answer", sdp: "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n" }); };
  this.setLocalDescription = function(d) { this.localDescription = d; return Promise.resolve(); };
  this.setRemoteDescription = function(d) { this.remoteDescription = d; return Promise.resolve(); };
  this.addIceCandidate = function() { return Promise.resolve(); };
  this.getStats = function() { return Promise.resolve(new Map()); };
  this.close = function() { this.connectionState = "closed"; };
  this.addEventListener = function() {};
  this.removeEventListener = function() {};
};
window.webkitRTCPeerConnection = window.RTCPeerConnection;
window.RTCSessionDescription = function(d) { Object.assign(this, d || {}); };
window.RTCIceCandidate = function(d) { Object.assign(this, d || {}); };

// JS-4: console buffering (вместо noop) — challenge JS может это проверять
(function() {
  var buf = [];
  ["log", "info", "warn", "error", "debug", "trace"].forEach(function(method) {
    var orig = console[method] || function() {};
    console[method] = function() {
      try {
        buf.push({ method: method, args: Array.prototype.slice.call(arguments), ts: Date.now() });
        if (buf.length > 200) buf.shift();
      } catch (e) {}
      return orig.apply(console, arguments);
    };
  });
  console.__buffer = function() { return buf.slice(); };
})();

// JS-7: DeviceMotion / DeviceOrientation stubs
window.DeviceMotionEvent = window.DeviceMotionEvent || function(t) { this.type = t || "devicemotion"; };
window.DeviceOrientationEvent = window.DeviceOrientationEvent || function(t) { this.type = t || "deviceorientation"; };
if (window.DeviceMotionEvent.requestPermission === undefined) {
  window.DeviceMotionEvent.requestPermission = function() { return Promise.resolve("granted"); };
}
if (window.DeviceOrientationEvent.requestPermission === undefined) {
  window.DeviceOrientationEvent.requestPermission = function() { return Promise.resolve("granted"); };
}

// JS: Permissions API
navigator.permissions = navigator.permissions || {
  query: function(desc) {
    var name = (desc && desc.name) || "";
    // Реалистичные дефолты для Android Telegram WebView
    var states = {
      notifications: "prompt",
      geolocation: "denied",
      camera: "denied",
      microphone: "denied",
      "persistent-storage": "granted",
      "background-sync": "granted",
    };
    var state = states[name] || "prompt";
    return Promise.resolve({ state: state, addEventListener: function() {}, removeEventListener: function() {} });
  }
};

// JS-10: Telegram.WebApp.HapticFeedback (имитация вибраций при тапах)
if (window.Telegram && window.Telegram.WebApp) {
  window.Telegram.WebApp.HapticFeedback = window.Telegram.WebApp.HapticFeedback || {
    impactOccurred: function(style) { return this; },  // light/medium/heavy/rigid/soft
    notificationOccurred: function(type) { return this; },  // error/success/warning
    selectionChanged: function() { return this; },
  };

  // JS-11: WebApp event registry
  if (!window.Telegram.WebApp.__events) {
    window.Telegram.WebApp.__events = {};
    window.Telegram.WebApp.onEvent = function(name, cb) {
      var e = window.Telegram.WebApp.__events;
      (e[name] = e[name] || []).push(cb);
    };
    window.Telegram.WebApp.offEvent = function(name, cb) {
      var arr = window.Telegram.WebApp.__events[name];
      if (!arr) return;
      var i = arr.indexOf(cb);
      if (i >= 0) arr.splice(i, 1);
    };
    // Хелпер для триггера событий из Python
    window.Telegram.WebApp.__triggerEvent = function(name, data) {
      var arr = window.Telegram.WebApp.__events[name];
      if (!arr) return;
      arr.forEach(function(cb) { try { cb(data); } catch (e) {} });
    };
  }
}

// window.history.length > 0 (real users navigated to here)
try {
  if (window.history && window.history.length === 0) {
    Object.defineProperty(window.history, "length", { get: function() { return 1 + (Date.now() % 3); } });
  }
} catch (e) {}

// Web Share API stub
navigator.share = navigator.share || function() { return Promise.resolve(); };
navigator.canShare = navigator.canShare || function() { return true; };

// Network Information API (real Android exposes effectiveType)
if (!navigator.connection) {
  navigator.connection = {
    effectiveType: "4g",
    type: "cellular",
    downlink: 10,
    rtt: 50,
    saveData: false,
    addEventListener: function() {},
    removeEventListener: function() {},
  };
}

})(_global);
