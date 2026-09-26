// Opens a real browser window for Douyin QR login, then sends the account's
// sec_uid plus www/creator cookies to the parent backend over stdout only —
// no credentials are written to disk by this script. The backend encrypts
// them with Windows DPAPI. status.json carries non-sensitive progress only.
// usage: node douyin-login.mjs <workDir>
import { chromium } from 'playwright';
import { mkdirSync, writeFileSync } from 'fs';
import path from 'path';

const workDir = process.argv[2];
if (!workDir) { console.error('missing workDir'); process.exit(2); }
mkdirSync(workDir, { recursive: true });
// ts must stay ISO-8601: the backend freshness check parses it with
// datetime.fromisoformat (epoch-ms numbers would be treated as stale).
const write = (phase, message) => writeFileSync(path.join(workDir, 'status.json'), JSON.stringify({ phase, message, ts: new Date().toISOString() }));

const LOGIN_TIMEOUT_MS = 6 * 60 * 1000;
// The sessionid cookie can appear at scan time while the web session behind
// it lands seconds later (or is briefly rejected as unverified), so one
// anonymous round does not mean the login failed — retry the ladder.
const EXTRACT_ATTEMPTS = 4;
const EXTRACT_GAP_MS = 10 * 1000;
const SEC_UID_RE = /\/user\/(?!self)([A-Za-z0-9_-]{20,})/;
const hasSession = async (ctx, domain) => (await ctx.cookies('https://' + domain)).some(c => c.name === 'sessionid');

// Legacy passport endpoint: needs no a_bogus signature, returns the session
// owner's sec_uid directly. Cheapest and most reliable probe.
async function passportSelfInfo(page) {
  try {
    const body = await page.evaluate(async () => {
      const r = await fetch('/passport/web/get_user_info/?from_login=0&aid=6383&app_id=1128', { credentials: 'include' });
      return await r.json();
    });
    if (body && /^[A-Za-z0-9_-]{20,}$/.test(body.sec_uid || '')) {
      return { sec_uid: body.sec_uid, nickname: body.name || null };
    }
  } catch {}
  return null;
}

// Current logged-in user via the web IM endpoint, fetched in page context so
// douyin cookies are attached. Unambiguous: describes the session owner only.
async function imSelfInfo(page) {
  try {
    const body = await page.evaluate(async () => {
      const r = await fetch('/aweme/v1/web/im/user/info/?device_platform=webapp&aid=6383&channel=Chrome_PC&cookie_enabled=true&browser_language=zh-CN&browser_platform=Win32&browser_name=Chrome&browser_version=126.0.0.0', { credentials: 'include' });
      return await r.json();
    });
    const user = body && body.data && body.data.user;
    if (body && body.status_code === 0 && user && /^[A-Za-z0-9_-]{20,}$/.test(user.sec_uid || '')) {
      return { sec_uid: user.sec_uid, nickname: user.nickname || null };
    }
  } catch {}
  return null;
}

// /user/self redirects to /user/<sec_uid> once the web session is live.
// The embedded-secUid fallback is ONLY trusted while the URL is still
// /user/self — on an anonymous feed page every secUid belongs to someone else.
async function selfProfile(page) {
  try {
    await page.goto('https://www.douyin.com/user/self', { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForURL(SEC_UID_RE, { timeout: 10000 }).catch(() => {});
    const fromUrl = page.url().match(SEC_UID_RE);
    if (fromUrl) return { sec_uid: fromUrl[1] };
    if (page.url().includes('/user/self')) {
      const m = (await page.content()).match(/"secUid":"(MS4[A-Za-z0-9_-]{16,})"/);
      if (m) return { sec_uid: m[1] };
    }
  } catch {}
  return null;
}

// The creator dashboard is rendered for the account owner, so its embedded
// secUid is unambiguous. Runs on its own SSO (creator.douyin.com) and reuses
// one tab across rounds; doubles as the creator-cookie visit.
async function creatorAccount(ctx, tab) {
  const p = tab || await ctx.newPage();
  try {
    await p.goto('https://creator.douyin.com/creator-micro/home', { waitUntil: 'domcontentloaded', timeout: 30000 });
    await p.waitForTimeout(6000);
    const html = await p.content();
    const m = html.match(/"secUid":"(MS4[A-Za-z0-9_-]{16,})"/) || html.match(/"sec_uid":"(MS4[A-Za-z0-9_-]{16,})"/);
    return { tab: p, sec_uid: m ? m[1] : null };
  } catch {
    return { tab: p, sec_uid: null };
  }
}

// Own-profile link from the homepage header/avatar region only — never from
// the feed, whose author links belong to other accounts.
async function headerSelfLink(page) {
  try {
    await page.goto('https://www.douyin.com/', { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForTimeout(3000);
    const href = await page.evaluate(() => {
      const own = /\/user\/(?!self)([A-Za-z0-9_-]{20,})/;
      const nodes = [...document.querySelectorAll('[data-e2e="profile-icon"] a[href*="/user/"], [data-e2e="profile-user"], header a[href*="/user/"]')];
      return nodes.map(a => a.getAttribute('href') || '').find(h => own.test(h)) || null;
    });
    const m = href && href.match(SEC_UID_RE);
    if (m) return { sec_uid: m[1] };
  } catch {}
  return null;
}

let ctx;
try {
  // Douyin risk-control rejects web sessions from automation-flagged browsers
  // (QR login "succeeds", then every page redirects to the anonymous feed),
  // so hide the two webdriver signals Playwright exposes by default.
  ctx = await chromium.launchPersistentContext(path.join(workDir, 'browser-profile'), {
    headless: false,
    viewport: { width: 1280, height: 860 },
    args: ['--lang=zh-CN', '--disable-blink-features=AutomationControlled'],
  });
  await ctx.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  });
  const page = ctx.pages()[0] || await ctx.newPage();
  write('open', '正在打开抖音登录页');
  await page.goto('https://www.douyin.com/', { waitUntil: 'domcontentloaded', timeout: 60000 });

  write('waiting_login', '请在弹出的浏览器窗口中扫码登录抖音');
  const deadline = Date.now() + LOGIN_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await hasSession(ctx, 'www.douyin.com')) break;
    await page.waitForTimeout(1500);
  }
  if (!(await hasSession(ctx, 'www.douyin.com'))) { write('failed', '登录超时（6分钟未完成扫码），请重新发起'); await ctx.close(); process.exit(1); }
  write('login_ok', '登录成功，正在读取账号信息');
  await page.waitForTimeout(2500);

  // sec_uid ladder: passport self info → IM self info → /user/self redirect →
  // creator dashboard → homepage header avatar. Every step identifies the
  // session owner only.
  let sec_uid = null, nickname = null, creatorTab = null, failures = [];
  for (let attempt = 1; attempt <= EXTRACT_ATTEMPTS && !sec_uid; attempt++) {
    if (attempt > 1) await page.waitForTimeout(EXTRACT_GAP_MS);
    const failed = [];
    const passport = await passportSelfInfo(page);
    if (passport) { sec_uid = passport.sec_uid; nickname = passport.nickname; } else failed.push('用户信息接口');
    if (!sec_uid) {
      const im = await imSelfInfo(page);
      if (im) { sec_uid = im.sec_uid; nickname = im.nickname; } else failed.push('IM接口');
    }
    if (!sec_uid) {
      const self = await selfProfile(page);
      if (self) sec_uid = self.sec_uid; else failed.push('主页跳转');
    }
    if (!sec_uid && (attempt === 2 || attempt === 4)) {
      const c = await creatorAccount(ctx, creatorTab);
      creatorTab = c.tab;
      if (c.sec_uid) sec_uid = c.sec_uid; else failed.push('创作者平台');
    }
    if (!sec_uid && (attempt === 2 || attempt === 4)) {
      const h = await headerSelfLink(page);
      if (h) sec_uid = h.sec_uid; else failed.push('头像链接');
    }
    if (!sec_uid && /verify|captcha/i.test(page.url())) {
      write('login_ok', '抖音要求安全验证：请在弹出的浏览器窗口中完成滑块验证');
      await page.waitForTimeout(15000);
    } else if (!sec_uid) {
      write('login_ok', `正在读取账号信息（第${attempt}/${EXTRACT_ATTEMPTS}次尝试）`);
    }
    failures = failed;
  }

  let creator_cookies = [];
  if (creatorTab) {
    creator_cookies = await ctx.cookies('https://creator.douyin.com');
    await creatorTab.close().catch(() => {});
  } else {
    try {
      write('creator', '正在连接抖音创作者平台（用于作品播放量）');
      const p2 = await ctx.newPage();
      await p2.goto('https://creator.douyin.com/creator-micro/home', { waitUntil: 'domcontentloaded', timeout: 30000 });
      await p2.waitForTimeout(6000);
      creator_cookies = await ctx.cookies('https://creator.douyin.com');
      await p2.close().catch(() => {});
    } catch { creator_cookies = []; }
  }

  if (!sec_uid) {
    let webdriver = 'unknown';
    try { webdriver = await page.evaluate(() => String(navigator.webdriver)); } catch {}
    write('failed', '登录成功但未能读取主页用户ID（已重试' + EXTRACT_ATTEMPTS + '次：' + failures.join('、') + '均未返回，页面停留在 ' + (page.url() || '').slice(0, 90) + '，webdriver=' + webdriver + '）。请重新发起一次扫码登录');
    await ctx.close();
    process.exit(1);
  }

  if (!nickname) {
    try {
      await page.goto('https://www.douyin.com/user/' + sec_uid, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForTimeout(2500);
      nickname = (await page.title()).split('的个人主页')[0].split('的主页')[0] || null;
    } catch {}
  }

  const web_cookies = await ctx.cookies('https://www.douyin.com');
  await ctx.close();
  write('saving', '登录完成，正在安全保存');
  // Single-line stdout payload consumed in memory by the backend; never persisted as plaintext.
  process.stdout.write('@@RESULT@@' + JSON.stringify({
    web_cookies: web_cookies.map(c => ({ name: c.name, value: c.value })),
    creator_cookies: creator_cookies.map(c => ({ name: c.name, value: c.value })),
    sec_uid, nickname,
  }) + '\n');
} catch (error) {
  write('failed', '登录过程出错：' + String(error && error.message || error).slice(0, 200));
  if (ctx) await ctx.close().catch(() => {});
  process.exit(1);
}
