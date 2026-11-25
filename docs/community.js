'use strict';
(() => {
  const node = id => document.getElementById(id);
  const make = (name, value) => Object.assign(document.createElement(name), {textContent: value});
  const display = (value, unit = '') => Number.isFinite(value) ? fmt(value, unit) : '—';
  const message = node('community-message');
  const projectTitle = node('title').textContent;
  const projectCopy = node('intro-copy').textContent;
  const options = {version: node('community-version'), profile: node('community-profile')};
  let origin, next, loaded = false, loading = false;
  const rows = [];
  const sortButtons = [...document.querySelectorAll('#community-data [data-sort]')];
  const collator = new Intl.Collator('en', {numeric: true, sensitivity: 'base'});
  const verdictRank = {ok: 0, stalls: 1, overloaded: 2, fault: 3};
  const sortKeys = {
    machine: row => row.item.cpu + ' ' + row.item.os,
    version: row => versionParts(row.item.version),
    profile: row => row.workload.join(' · '),
    verdict: row => verdictRank[row.run.verdict],
  };
  let sortKey, ascending;

  function versionParts(version) {
    const match = /^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$/.exec(version);
    return match ? [Number(match[1]), Number(match[2]), Number(match[3]),
      {a: 0, b: 1, rc: 2}[match[4]] ?? 3, Number(match[5] || 0)] : null;
  }

  function compare(left, right) {
    if (Array.isArray(left)) {
      return left.map((part, index) => part - right[index]).find(diff => diff !== 0) ?? 0;
    }
    return typeof left === 'number' ? left - right : collator.compare(left, right);
  }

  function order(matches) {
    if (!sortKey) return;
    const value = sortKeys[sortKey] || (row => row.run[sortKey]);
    matches.sort((a, b) => {
      const left = value(a), right = value(b);
      if (left == null) return right == null ? 0 : 1;
      if (right == null) return -1;
      return compare(left, right) * (ascending ? 1 : -1);
    });
  }

  function profile(item, run) {
    return [
      run.map + ' / ' + run.scenario,
      run.arenas + ' arena' + (run.arenas === 1 ? '' : 's') + ' × ' + run.tanks + ' tanks',
      run.seconds + ' s / ' + item.warmup + ' s warm-up',
      item.engine + ' / ' + item.backend + ' / ' + item.input_pattern,
      item.game_build,
    ];
  }

  function filterOptions(select, values, all) {
    const before = select.value;
    const unique = [...new Set(values)].sort();
    select.replaceChildren(new Option(all, ''), ...unique.map(value => new Option(value, value)));
    if (unique.includes(before)) select.value = before;
  }

  function draw() {
    const query = node('community-search').value.trim().toLowerCase();
    const matches = rows.filter(({item, workload}) =>
      (!options.version.value || options.version.value === item.version) &&
      (!options.profile.value || options.profile.value === workload.join(' · ')) &&
      (!query || (item.cpu + ' ' + item.os).toLowerCase().includes(query)));
    order(matches);
    node('community-body').replaceChildren(...matches.map(({item, run, workload}) => {
      const row = make('tr', '');
      const machine = make('th', item.cpu);
      machine.scope = 'row';
      const hardware = make('span', item.os + ' · ' + item.logical_cpus + ' logical CPUs · ' + display(item.ram_gib, 'GiB'));
      hardware.className = 'commit';
      machine.append(hardware);
      const load = make('td', workload.slice(0, 2).join(' · '));
      const details = make('span', workload.slice(2).join(' · '));
      details.className = 'commit';
      load.append(details);
      row.append(machine, make('td', item.version), load, ...[
        display(run.hz), display(run.input_ms, 'ms'), display(run.tick_ms, 'ms'),
        display(run.cpu_cores), display(run.rss_mib, 'MiB'), display(run.udp_drops),
        labels[run.verdict] || run.verdict,
      ].map(value => make('td', value)));
      return row;
    }));
    node('community-data').hidden = matches.length === 0;
    message.textContent = rows.length === 0 ? 'There are no community results yet.'
      : matches.length ? matches.length + ' of ' + rows.length + ' loaded runs'
      : 'No loaded runs match these filters.';
  }

  async function loadPage() {
    if (loading) return;
    loading = true;
    node('community-more').disabled = true;
    message.textContent = 'Loading community results…';
    try {
      if (!origin) {
        const config = await fetch('data/community-api.json', {cache: 'no-store'});
        if (config.status === 404) {
          message.textContent = 'Community submissions are not connected yet.';
          return;
        }
        if (!config.ok) throw new Error();
        const url = new URL((await config.json()).base_url);
        const local = url.protocol === 'http:' && ['localhost','127.0.0.1'].includes(location.hostname)
          && url.hostname === '127.0.0.1';
        if (url.protocol !== 'https:' && !local) {
          throw new Error();
        }
        if (url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error();
        origin = url.origin;
      }
      const query = next ? '?before=' + next : '';
      const response = await fetch(origin + '/v1/results' + query);
      if (!response.ok) throw new Error();
      const page = await response.json();
      for (const item of page.items) for (const run of item.runs) {
        rows.push({item, run, workload: profile(item, run)});
      }
      next = page.next;
      filterOptions(options.version, rows.map(row => row.item.version), 'All versions');
      filterOptions(options.profile, rows.map(row => row.workload.join(' · ')), 'All workloads');
      node('community-filters').hidden = rows.length === 0;
      node('community-more').hidden = !next;
      loaded = true;
      draw();
    } catch {
      message.textContent = 'Could not load community results. Select Community to retry.';
    } finally {
      loading = false;
      node('community-more').disabled = false;
    }
  }

  function select(community) {
    node('project-tab').setAttribute('aria-pressed', String(!community));
    node('community-tab').setAttribute('aria-pressed', String(community));
    node('project-panel').hidden = community;
    node('community-panel').hidden = !community;
    node('title').textContent = community ? 'Community benchmarks' : projectTitle;
    node('intro-copy').textContent = community
      ? 'Explore player-submitted runs across different machines and Solbreaker versions.'
      : projectCopy;
    if (community && !loaded) loadPage();
  }
  node('project-tab').addEventListener('click', () => select(false));
  node('community-tab').addEventListener('click', () => select(true));
  node('community-more').addEventListener('click', loadPage);
  node('community-search').addEventListener('input', draw);
  options.version.addEventListener('change', draw);
  options.profile.addEventListener('change', draw);
  for (const button of sortButtons) button.addEventListener('click', () => {
    const key = button.dataset.sort;
    ascending = sortKey === key ? !ascending : key !== 'hz' && key !== 'version';
    sortKey = key;
    for (const candidate of sortButtons) candidate.parentElement.removeAttribute('aria-sort');
    button.parentElement.setAttribute('aria-sort', ascending ? 'ascending' : 'descending');
    draw();
  });
  if (location.hash === '#community') select(true);
})();
