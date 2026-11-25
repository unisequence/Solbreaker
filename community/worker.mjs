const SIZE_LIMIT = 32768;
const MAX_RUNS = 32;
const PAGE_SIZE = 30;
const label = (value, pattern) => typeof value === 'string' && pattern.test(value);
const whole = (value, min, max) => Number.isSafeInteger(value) && value >= min && value <= max;
const measure = (value, max) => value == null || (Number.isFinite(value) && value >= 0 && value <= max);

function validate(row) {
  const keys = ['schema', 'version', 'game_build', 'cpu', 'os', 'logical_cpus', 'ram_gib', 'engine',
    'backend', 'input_pattern', 'warmup', 'runs'];
  if (!row || Object.keys(row).length !== keys.length || keys.some(key => !(key in row))
      || row.schema !== 1
      || !label(row.version, /^\d{1,4}\.\d{1,4}\.\d{1,4}(?:(?:a|b|rc)\d{1,4})?$/)
      || !label(row.game_build, /^\d{1,4}(?:\.\d{1,8}){2,5}_\d{1,12}$/)
      || !label(row.cpu, /^[\p{L}\p{N} ._()+®™-]{1,100}$/u)
      || !['Linux', 'Windows'].includes(row.os)
      || !whole(row.logical_cpus, 1, 4096)
      || !measure(row.ram_gib, 65536)
      || !['owner', 'in-process'].includes(row.engine)
      || !['rapier', 'bullet'].includes(row.backend)
      || !['burst', 'spread'].includes(row.input_pattern)
      || !whole(row.warmup, 0, 120)
      || !Array.isArray(row.runs) || row.runs.length < 1 || row.runs.length > MAX_RUNS) {
    throw new TypeError('Invalid benchmark summary');
  }
  const runs = row.runs.map(run => {
    const runKeys = ['map', 'scenario', 'arenas', 'tanks', 'seconds', 'hz', 'input_ms', 'tick_ms',
      'cpu_cores', 'rss_mib', 'udp_drops', 'verdict'];
    if (!run || Object.keys(run).length !== runKeys.length || runKeys.some(key => !(key in run))
        || !label(run.map, /^[a-z0-9_]{1,64}$/)
        || !['solo', 'bots', 'players'].includes(run.scenario)
        || !whole(run.arenas, 1, 16) || !whole(run.tanks, 2, 14)
        || !whole(run.seconds, 5, 3600)
        || !measure(run.hz, 1000) || run.hz == null
        || !measure(run.input_ms, 3600000) || !measure(run.tick_ms, 3600000)
        || !measure(run.cpu_cores, 4096) || !measure(run.rss_mib, 67108864)
        || !(run.udp_drops === null || whole(run.udp_drops, 0, 1000000000))
        || !['ok', 'stalls', 'overloaded', 'fault'].includes(run.verdict)) {
      throw new TypeError('Invalid benchmark run');
    }
    return Object.fromEntries(runKeys.map(key => [key, run[key]]));
  });
  return Object.fromEntries(keys.map(key => [key, key === 'runs' ? runs : row[key]]));
}

function json(value, status = 200, options = {}) {
  return Response.json(value, {status, headers: {
    'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*',
    'X-Content-Type-Options': 'nosniff', ...options,
  }});
}

async function readJson(request) {
  const reader = request.body?.getReader();
  if (!reader) throw new TypeError('Missing request body');
  const decoder = new TextDecoder('utf-8', {fatal: true});
  let data = '', length = 0;
  for (;;) {
    const {done, value} = await reader.read();
    if (done) return JSON.parse(data + decoder.decode());
    length += value.byteLength;
    if (length > SIZE_LIMIT) {
      await reader.cancel();
      throw new RangeError('Summary exceeds size limit');
    }
    data += decoder.decode(value, {stream: true});
  }
}

async function submit(request, env) {
  if (!/^application\/json(?:\s*;|$)/i.test(request.headers.get('Content-Type') || '')) {
    return json({error: 'Expected JSON'}, 415);
  }
  if (Number(request.headers.get('Content-Length') || 0) > SIZE_LIMIT) {
    return json({error: 'Summary exceeds size limit'}, 413);
  }
  const ip = request.headers.get('CF-Connecting-IP') || 'local';
  if (!(await env.SUBMISSIONS.limit({key: ip})).success
      || !(await env.GLOBAL_SUBMISSIONS.limit({key: 'submit'})).success) {
    return json({error: 'Too many submissions'}, 429, {'Retry-After': '60'});
  }
  let payload;
  try {
    payload = JSON.stringify(validate(await readJson(request)));
  } catch (error) {
    return json({error: error.message}, error instanceof RangeError ? 413 : 400);
  }
  const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(payload)))]
    .map(value => value.toString(16).padStart(2, '0')).join('');
  const insert = await env.RESULTS.prepare(
    'INSERT OR IGNORE INTO benchmark_results (digest, payload) VALUES (?, ?)'
  ).bind(digest, payload).run();
  const duplicate = insert.meta.changes === 0;
  return json({id: digest, duplicate}, duplicate ? 200 : 201);
}

async function list(url, env) {
  const cursor = url.searchParams.get('before');
  if (cursor !== null && (!/^\d{1,15}$/.test(cursor) || !whole(Number(cursor), 1, Number.MAX_SAFE_INTEGER))) {
    return json({error: 'Invalid page cursor'}, 400);
  }
  const query = cursor === null
    ? env.RESULTS.prepare('SELECT sequence, digest, submitted_at, payload FROM benchmark_results ORDER BY sequence DESC LIMIT ?').bind(PAGE_SIZE + 1)
    : env.RESULTS.prepare('SELECT sequence, digest, submitted_at, payload FROM benchmark_results WHERE sequence < ? ORDER BY sequence DESC LIMIT ?')
      .bind(Number(cursor), PAGE_SIZE + 1);
  const {results} = await query.all();
  const items = results.slice(0, PAGE_SIZE);
  return json({
    items: items.map(row => ({id: row.digest, submitted_at: row.submitted_at, ...JSON.parse(row.payload)})),
    next: results.length > PAGE_SIZE ? items.at(-1).sequence : null,
  }, 200, {'Cache-Control': 'public, max-age=15'});
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== '/v1/results') return json({error: 'Not found'}, 404);
    try {
      if (request.method === 'GET') return await list(url, env);
      if (request.method === 'POST') return await submit(request, env);
      return json({error: 'Method not allowed'}, 405, {'Allow': 'GET, POST'});
    } catch (error) {
      console.error('Benchmark storage request failed', error.name);
      return json({error: 'Benchmark service unavailable'}, 503);
    }
  }
};
