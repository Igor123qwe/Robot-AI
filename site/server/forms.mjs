#!/usr/bin/env node
import http from 'node:http';

/**
 * Приём заявок с форм сайта (§8.4).
 *
 * Сайт статический, поэтому единственная серверная часть — этот обработчик.
 * Он принимает JSON, отсеивает ботов honeypot-полем и лимитом по IP,
 * отправляет заявку в Telegram и дублирует на почту. Зависимостей нет:
 * Telegram — обычный fetch, почта — опциональный nodemailer, если он
 * установлен и настроен.
 *
 * Запуск: node server/forms.mjs (см. systemd-юнит в README).
 *
 * Переменные окружения:
 *   PORT                  порт (по умолчанию 8787)
 *   TELEGRAM_BOT_TOKEN    токен бота
 *   TELEGRAM_CHAT_ID      куда слать заявки
 *   ALLOWED_ORIGIN        источник для CORS, по умолчанию запросы только с того же домена
 *   SMTP_URL              smtps://user:pass@host:465 — если нужно дублирование на почту
 *   MAIL_TO, MAIL_FROM    адреса для дубля
 *   RATE_LIMIT            заявок с одного IP за окно (по умолчанию 5)
 *   RATE_WINDOW_MS        длина окна в миллисекундах (по умолчанию 600000)
 */

const PORT = Number(process.env.PORT ?? 8787);
const TOKEN = process.env.TELEGRAM_BOT_TOKEN ?? '';
const CHAT_ID = process.env.TELEGRAM_CHAT_ID ?? '';
const ALLOWED_ORIGIN = process.env.ALLOWED_ORIGIN ?? '';
const RATE_LIMIT = Number(process.env.RATE_LIMIT ?? 5);
const RATE_WINDOW_MS = Number(process.env.RATE_WINDOW_MS ?? 10 * 60 * 1000);
const MAX_BODY = 16 * 1024;

/** @type {Map<string, number[]>} IP → отметки времени последних заявок */
const hits = new Map();

const clientIp = (req) => {
  const forwarded = req.headers['x-forwarded-for'];
  if (typeof forwarded === 'string' && forwarded.length > 0) return forwarded.split(',')[0].trim();
  return req.socket.remoteAddress ?? 'unknown';
};

const rateLimited = (ip) => {
  const now = Date.now();
  const recent = (hits.get(ip) ?? []).filter((time) => now - time < RATE_WINDOW_MS);
  recent.push(now);
  hits.set(ip, recent);
  // Чистим карту, чтобы она не росла бесконечно на долгоживущем процессе
  if (hits.size > 5000) {
    for (const [key, times] of hits) {
      if (times.every((time) => now - time > RATE_WINDOW_MS)) hits.delete(key);
    }
  }
  return recent.length > RATE_LIMIT;
};

/** Срезаем управляющие символы: они ломают разметку сообщения в Telegram */
const clean = (value, limit = 500) =>
  String(value ?? '')
    .replace(/[\u0000-\u001F\u007F]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, limit);

const escapeHtml = (value) =>
  value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

const MARKETPLACES = {
  ozon: 'Ozon',
  wildberries: 'Wildberries',
  'yandex-market': 'Яндекс Маркет',
  several: 'Несколько площадок',
  none: 'Пока не продаёт',
};

const buildMessage = (lead) => {
  const lines = [
    '<b>Заявка с сайта</b>',
    `<b>Имя:</b> ${escapeHtml(lead.name)}`,
    `<b>Контакт:</b> ${escapeHtml(lead.contact)}`,
  ];
  if (lead.marketplace) lines.push(`<b>Площадка:</b> ${escapeHtml(MARKETPLACES[lead.marketplace] ?? lead.marketplace)}`);
  if (lead.comment) lines.push(`<b>Комментарий:</b> ${escapeHtml(lead.comment)}`);
  lines.push(`<b>Форма:</b> ${escapeHtml(lead.source)}`);
  lines.push(`<b>Страница:</b> ${escapeHtml(lead.page)}`);
  if (lead.referrer) lines.push(`<b>Реферер:</b> ${escapeHtml(lead.referrer)}`);
  if (lead.landing && lead.landing !== lead.page) lines.push(`<b>Точка входа:</b> ${escapeHtml(lead.landing)}`);
  if (lead.utm) lines.push(`<b>UTM:</b> ${escapeHtml(lead.utm)}`);
  lines.push(`<b>Время:</b> ${new Date().toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' })} МСК`);
  return lines.join('\n');
};

const sendTelegram = async (text) => {
  if (!TOKEN || !CHAT_ID) throw new Error('Telegram не настроен: нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID');
  const response = await fetch(`https://api.telegram.org/bot${TOKEN}/sendMessage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      chat_id: CHAT_ID,
      text,
      parse_mode: 'HTML',
      disable_web_page_preview: true,
    }),
  });
  if (!response.ok) throw new Error(`Telegram ответил ${response.status}: ${(await response.text()).slice(0, 200)}`);
};

let mailer = null;
const sendMail = async (text) => {
  if (!process.env.SMTP_URL || !process.env.MAIL_TO) return;
  if (!mailer) {
    const nodemailer = await import('nodemailer').catch(() => null);
    if (!nodemailer) {
      console.warn('[forms] nodemailer не установлен — дублирование на почту пропущено');
      return;
    }
    mailer = nodemailer.default.createTransport(process.env.SMTP_URL);
  }
  await mailer.sendMail({
    from: process.env.MAIL_FROM ?? process.env.MAIL_TO,
    to: process.env.MAIL_TO,
    subject: 'Заявка с сайта',
    text: text.replace(/<[^>]+>/g, ''),
    html: text.replace(/\n/g, '<br>'),
  });
};

const json = (res, status, payload, origin) => {
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    ...(origin ? { 'Access-Control-Allow-Origin': origin, Vary: 'Origin' } : {}),
  });
  res.end(JSON.stringify(payload));
};

const server = http.createServer((req, res) => {
  const origin = ALLOWED_ORIGIN && req.headers.origin === ALLOWED_ORIGIN ? ALLOWED_ORIGIN : '';

  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      ...(origin ? { 'Access-Control-Allow-Origin': origin } : {}),
      'Access-Control-Allow-Methods': 'POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      'Access-Control-Max-Age': '86400',
    });
    res.end();
    return;
  }

  if (req.method === 'GET' && req.url === '/healthz') {
    json(res, 200, { ok: true, telegram: Boolean(TOKEN && CHAT_ID) });
    return;
  }

  if (req.method !== 'POST') {
    json(res, 405, { ok: false, error: 'method not allowed' }, origin);
    return;
  }

  const ip = clientIp(req);
  let raw = '';
  let aborted = false;

  req.on('data', (chunk) => {
    raw += chunk;
    if (raw.length > MAX_BODY) {
      aborted = true;
      json(res, 413, { ok: false, error: 'payload too large' }, origin);
      req.destroy();
    }
  });

  req.on('end', async () => {
    if (aborted) return;

    let body;
    try {
      body = JSON.parse(raw || '{}');
    } catch {
      json(res, 400, { ok: false, error: 'bad json' }, origin);
      return;
    }

    // Honeypot: настоящее поле скрыто от людей, боты его заполняют.
    // Отвечаем успехом, чтобы бот не подбирал обход.
    if (clean(body.company)) {
      console.log(`[forms] honeypot сработал, ip=${ip}`);
      json(res, 200, { ok: true }, origin);
      return;
    }

    const lead = {
      name: clean(body.name, 80),
      contact: clean(body.contact, 80),
      marketplace: clean(body.marketplace, 40),
      comment: clean(body.comment, 1200),
      source: clean(body.source, 60) || 'unknown',
      page: clean(body.page, 300),
      referrer: clean(body.referrer, 300),
      landing: clean(body.landing, 300),
      utm: clean(
        typeof body.utm === 'object' && body.utm
          ? Object.entries(body.utm)
              .map(([key, value]) => `${key}=${value}`)
              .join(' ')
          : body.utm,
        300,
      ),
    };

    if (lead.name.length < 2 || lead.contact.length < 4) {
      json(res, 422, { ok: false, error: 'not enough data' }, origin);
      return;
    }

    if (rateLimited(ip)) {
      console.warn(`[forms] лимит заявок исчерпан, ip=${ip}`);
      json(res, 429, { ok: false, error: 'too many requests' }, origin);
      return;
    }

    const message = buildMessage(lead);

    try {
      await sendTelegram(message);
    } catch (error) {
      console.error(`[forms] Telegram: ${error.message}`);
      json(res, 502, { ok: false, error: 'delivery failed' }, origin);
      return;
    }

    // Почта — дубль: её падение не должно ломать заявку, она уже в Telegram
    sendMail(message).catch((error) => console.error(`[forms] почта: ${error.message}`));

    console.log(`[forms] заявка принята: source=${lead.source} ip=${ip}`);
    json(res, 200, { ok: true }, origin);
  });
});

server.listen(PORT, () => {
  console.log(`[forms] слушаю порт ${PORT}, Telegram ${TOKEN && CHAT_ID ? 'настроен' : 'НЕ настроен'}`);
});
