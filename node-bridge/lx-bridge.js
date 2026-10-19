// 洛雪音源桥接服务 (lx-bridge)
// 在 Node.js 中模拟洛雪桌面端的 globalThis.lx 运行时，
// 加载用户导入的洛雪 .js 音源脚本，并通过 HTTP 暴露给 Python 后端。
//
// 洛雪音源脚本契约：
//   - 通过 globalThis.lx.request / on / send / utils 与宿主交互
//   - send(EVENT_NAMES.inited, { sources, openDevTools }) 声明支持的源
//   - on(EVENT_NAMES.request, ({source, action, info}) => Promise) 处理请求
//     action 可为: musicUrl / lyric / pic
//
// 本 shim 实现：
//   1. lx.request(url, options, callback) -> 返回可取消函数；callback(err, resp, body)
//   2. lx.utils.buffer.from / lx.utils.crypto 等常用工具（音源脚本常见依赖）
//   3. 记录每个源的 inited 信息与 request 回调，供 HTTP 路由调用

'use strict';

const http = require('http');
const https = require('https');
const { URL } = require('url');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const crypto = require('crypto');

// ---------- 全局状态 ----------
const SOURCES = new Map();   // sourceKey -> { name, type, actions, qualitys, requestHandler }
const PORT = parseInt(process.env.LX_BRIDGE_PORT || '2692', 10);
const SOURCES_DIR = process.env.SOURCES_DIR || '/data/sources';

// ---------- lx.utils ----------
// 完整实现洛雪官方自定义源文档中列出的全部工具方法
const zlib = require('zlib');

const lxUtils = {
  buffer: {
    from: (data, encoding) => Buffer.from(data, encoding),
    bufToString: (buffer, format) => Buffer.from(buffer).toString(format || 'utf8'),
  },
  crypto: {
    md5: (str) => crypto.createHash('md5').update(str).digest('hex'),
    randomBytes: (size) => crypto.randomBytes(size).toString('hex'),
    aesEncrypt: (buffer, mode, key, iv) => {
      // 洛雪: aesEncrypt(buffer, mode, key, iv)
      // mode 形如 'aes-128-ecb' / 'aes-128-cbc' 等
      const buf = Buffer.isBuffer(buffer) ? buffer : Buffer.from(buffer);
      const keyBuf = Buffer.isBuffer(key) ? key : Buffer.from(key);
      const ivBuf = Buffer.isBuffer(iv) ? iv : Buffer.from(iv || '');
      const algo = String(mode).replace('aes-', 'aes-'); // 保持原样
      try {
        const cipher = crypto.createCipheriv(algo, keyBuf, ivBuf.length ? ivBuf : null);
        cipher.setAutoPadding(true);
        return Buffer.concat([cipher.update(buf), cipher.final()]);
      } catch (e) {
        // 兼容无 iv 的 ecb 模式
        const cipher = crypto.createCipheriv(algo, keyBuf, null);
        cipher.setAutoPadding(true);
        return Buffer.concat([cipher.update(buf), cipher.final()]);
      }
    },
    rsaEncrypt: (buffer, key) => {
      const buf = Buffer.isBuffer(buffer) ? buffer : Buffer.from(buffer);
      try {
        const pub = crypto.createPublicKey(key);
        return crypto.publicEncrypt({ key: pub, padding: crypto.constants.RSA_PKCS1_PADDING }, buf);
      } catch (e) {
        throw e;
      }
    },
  },
  zlib: {
    inflate: (buffer) => new Promise((resolve, reject) => {
      zlib.inflate(Buffer.from(buffer), (err, result) => {
        if (err) return reject(err);
        resolve(result);
      });
    }),
    deflate: (buffer) => new Promise((resolve, reject) => {
      zlib.deflate(Buffer.from(buffer), (err, result) => {
        if (err) return reject(err);
        resolve(result);
      });
    }),
  },
};

// ---------- lx.request ----------
// options 可用: method / headers / body / form / formData / timeout
// callback: (err, resp, body)
function lxRequest(url, options, callback) {
  if (typeof options === 'function') {
    callback = options;
    options = {};
  }
  options = options || {};
  const method = (options.method || 'GET').toUpperCase();
  const timeout = options.timeout || 20000;

  let reqUrl;
  try {
    reqUrl = new URL(url);
  } catch (e) {
    callback(new Error('invalid url: ' + url));
    return () => {};
  }

  const lib = reqUrl.protocol === 'https:' ? https : http;
  const headers = Object.assign({}, options.headers);

  let bodyPayload = null;
  if (options.body) {
    bodyPayload = options.body;
  } else if (options.form) {
    bodyPayload = new URLSearchParams(options.form).toString();
    headers['Content-Type'] = 'application/x-www-form-urlencoded';
  } else if (options.formData) {
    bodyPayload = JSON.stringify(options.formData);
    headers['Content-Type'] = 'application/json';
  }
  if (bodyPayload && !headers['Content-Length']) {
    headers['Content-Length'] = Buffer.byteLength(bodyPayload);
  }

  const reqOptions = {
    method,
    headers,
  };

  let settled = false;
  const finish = (err, resp, body) => {
    if (settled) return;
    settled = true;
    callback(err, resp, body);
  };

  const req = lib.request(reqUrl, reqOptions, (res) => {
    const chunks = [];
    res.on('data', (c) => chunks.push(c));
    res.on('end', () => {
      const buf = Buffer.concat(chunks);
      // 洛雪脚本通常期望 body 为字符串（resp.body）
      const respObj = {
        statusCode: res.statusCode,
        headers: res.headers,
        body: buf.toString('utf8'),
        rawBody: buf,
      };
      finish(null, respObj, respObj.body);
    });
  });

  req.on('error', (err) => finish(err));
  req.setTimeout(timeout, () => {
    req.destroy(new Error('timeout'));
  });

  if (bodyPayload) req.write(bodyPayload);
  req.end();

  return () => {
    if (!settled) req.destroy();
  };
}

// ---------- lx.on / lx.send ----------
const EVENT_NAMES = {
  inited: 'inited',
  request: 'request',
  updateAlert: 'updateAlert',
};

let pendingRequestHandler = null;

function lxOn(eventName, handler) {
  if (eventName === EVENT_NAMES.request) {
    pendingRequestHandler = handler;
  }
  // 其它事件暂不处理
}

function lxSend(eventName, datas) {
  if (eventName === EVENT_NAMES.inited) {
    const sources = datas.sources || {};
    for (const [key, val] of Object.entries(sources)) {
      SOURCES.set(key, {
        key,
        name: val.name || key,
        type: val.type || 'music',
        actions: val.actions || [],
        qualitys: val.qualitys || [],
        requestHandler: pendingRequestHandler, // 音源脚本通常在 inited 前注册 request
      });
    }
    console.log(`[lx-bridge] 音源初始化完成: ${Object.keys(sources).join(', ')}`);
  }
}

// ---------- 构造 globalThis.lx ----------
const lx = {
  version: '1.0.0',
  env: 'desktop',
  currentScriptInfo: { name: '', description: '', version: '', author: '', homepage: '', rawScript: '' },
  EVENT_NAMES,
  request: lxRequest,
  on: lxOn,
  send: lxSend,
  utils: lxUtils,
};

// Proxy 兜底：访问未定义的方法时给出明确日志，便于诊断音源脚本依赖
const lxProxy = new Proxy(lx, {
  get(target, prop) {
    if (prop in target) return target[prop];
    console.warn(`[lx-bridge] 音源脚本访问了未实现的 lx.${String(prop)}，返回 undefined`);
    return undefined;
  },
});

// ---------- 加载音源脚本 ----------
function loadSourceScript(scriptPath) {
  const raw = fs.readFileSync(scriptPath, 'utf8');
  // 解析头部注释
  const info = { name: '', description: '', version: '', author: '', homepage: '', rawScript: raw };
  const headerMatch = raw.match(/\/\*!?([\s\S]*?)\*\//);
  if (headerMatch) {
    const header = headerMatch[1];
    const get = (key) => {
      const m = header.match(new RegExp('@' + key + '\\s+(.+)'));
      return m ? m[1].trim() : '';
    };
    info.name = get('name') || path.basename(scriptPath);
    info.description = get('description');
    info.version = get('version');
    info.author = get('author');
    info.homepage = get('homepage');
  }
  lx.currentScriptInfo = info;

  const sandbox = {
    globalThis: {},
    console,
    Buffer,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    URL,
    URLSearchParams,
    TextEncoder,
    TextDecoder,
    Math,
    JSON,
    Promise,
  };
  sandbox.globalThis.lx = lxProxy;
  sandbox.global = sandbox.globalThis;
  sandbox.window = sandbox.globalThis;

  try {
    const context = vm.createContext(sandbox);
    const wrapped = `(function(globalThis){ ${raw} \n})(globalThis);`;
    vm.runInContext(wrapped, context, { timeout: 10000 });
    console.log(`[lx-bridge] 已加载音源: ${info.name} (${path.basename(scriptPath)})`);
    return true;
  } catch (e) {
    console.error(`[lx-bridge] 加载音源失败 ${scriptPath}: ${e.message}`);
    return false;
  }
}

function loadAllSources() {
  SOURCES.clear();
  pendingRequestHandler = null;
  if (!fs.existsSync(SOURCES_DIR)) return;
  const files = fs.readdirSync(SOURCES_DIR).filter((f) => f.endsWith('.js'));
  for (const f of files) {
    loadSourceScript(path.join(SOURCES_DIR, f));
  }
}

// ---------- HTTP 路由 ----------
/**
 * 判断音源脚本的返回是否「有效」，决定是否需要轮询下一个音源。
 * 取链场景必须以能否拿到 http 直链为准 —— 只返回空对象不代表成功。
 */
function isUsefulResult(action, result) {
  if (action === 'musicUrl') {
    if (typeof result === 'string' && result.startsWith('http')) return true;
    if (result && typeof result === 'object') {
      const u = result.url || result.musicUrl || result.data || result.src;
      if (typeof u === 'string' && u.startsWith('http')) return true;
    }
    return false;
  }
  if (result === null || result === undefined) return false;
  if (typeof result === 'string') return result.trim().length > 0;
  if (typeof result === 'object') return Object.keys(result).length > 0;
  return !!result;
}

async function handleRequest(req, res) {
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  const u = new URL(req.url, 'http://localhost');
  const send = (code, obj) => {
    res.statusCode = code;
    res.end(JSON.stringify(obj));
  };

  try {
    if (u.pathname === '/health') {
      return send(200, { success: true, sources: [...SOURCES.keys()] });
    }

    if (u.pathname === '/sources') {
      return send(200, {
        success: true,
        data: [...SOURCES.values()].map((s) => ({
          key: s.key, name: s.name, actions: s.actions, qualitys: s.qualitys,
        })),
      });
    }

    if (u.pathname === '/reload') {
      loadAllSources();
      return send(200, { success: true, data: [...SOURCES.keys()] });
    }

    if (u.pathname === '/search') {
      // 音源脚本本身不提供 search，search 由 Python 端内置数据源承担。
      // 这里仅桥接 lyric / pic / musicUrl。
      return send(200, { success: false, message: 'search 由内置数据源提供' });
    }

    // action 请求: /action?source=tx&action=musicUrl&musicInfo=&quality=&script=
    // 注意语义区分：
    //   source = 平台代号（wy / tx / kg / kw / mg），洛雪协议要求如此
    //   script = 音源脚本 key（可选，留空则按序轮询全部已加载音源）
    if (u.pathname === '/action') {
      const source = u.searchParams.get('source');
      const action = u.searchParams.get('action');
      const scriptKey = u.searchParams.get('script') || null;
      const musicInfoStr = u.searchParams.get('musicInfo') || '{}';
      const quality = u.searchParams.get('quality') || null;

      let musicInfo;
      try {
        musicInfo = JSON.parse(musicInfoStr);
      } catch (e) {
        return send(400, { success: false, message: 'musicInfo 解析失败' });
      }

      let list;
      if (scriptKey) {
        const one = SOURCES.get(scriptKey);
        if (!one || typeof one.requestHandler !== 'function') {
          return send(404, { success: false, message: `音源 ${scriptKey} 不存在或未初始化` });
        }
        list = [one];
      } else {
        list = [...SOURCES.values()].filter((s) => typeof s.requestHandler === 'function');
      }
      if (!list.length) {
        return send(404, { success: false, message: '没有可用音源脚本' });
      }

      const info = { type: quality, musicInfo };
      const errors = [];
      for (const src of list) {
        try {
          const result = await src.requestHandler({ source, action, info });
          if (isUsefulResult(action, result)) {
            return send(200, { success: true, data: result, script: src.key });
          }
          errors.push(`${src.key}: 返回空结果`);
        } catch (e) {
          errors.push(`${src.key}: ${e && e.message ? e.message : e}`);
        }
      }
      return send(502, {
        success: false,
        message: `全部音源均未返回有效结果（${action}）`,
        errors,
      });
    }

    // 读取请求体（用于 POST /action）
    return send(404, { success: false, message: 'not found' });
  } catch (e) {
    return send(500, { success: false, message: e.message });
  }
}

// 支持 POST body（扩展读取）
const server = http.createServer(async (req, res) => {
  // 简单处理 GET 即可，音源 action 均为 GET 传参
  await handleRequest(req, res);
});

loadAllSources();

// 捕获未处理的 Promise rejection，避免音源脚本内部自检失败导致进程退出
process.on('unhandledRejection', (reason) => {
  console.warn('[lx-bridge] 未处理的 Promise rejection（忽略）:', (reason && reason.message) || reason);
});
process.on('uncaughtException', (err) => {
  console.warn('[lx-bridge] 未捕获异常（忽略）:', err && err.message);
});

server.listen(PORT, () => {
  console.log(`[lx-bridge] 服务已启动，监听端口 ${PORT}，音源目录 ${SOURCES_DIR}`);
});
