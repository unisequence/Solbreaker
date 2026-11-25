'use strict';
const get = id => document.getElementById(id);
const stats = {
  hz: ['Lowest simulation frequency', 'Hz', 'Higher is better'],
  input_ms: ['Worst arena input p99', 'ms', 'Lower is better'],
  tick_ms: ['Server tick p99', 'ms', 'Lower is better'],
  cpu: ['Mean CPU use', 'cores', 'Lower is better'],
  ram_mib: ['Resident memory at finish', 'MiB', 'Lower is better'],
  drops: ['UDP receive drops', '', 'Fewer is better']
};
const labels = {ok: 'Passed', stalls: 'Timing stalls', overloaded: 'Overloaded', fault: 'Server fault', error: 'Failed'};
const number = new Intl.NumberFormat('en', {maximumFractionDigits: 2});
const fmt = (value, unit = '') => Number.isFinite(value) ? number.format(value) + (unit ? ' ' + unit : '') : '—';
const el = (tag, value) => Object.assign(document.createElement(tag), {textContent: value});
function link(label, href) {
  const item = el('a', label);
  item.href = href;
  return item;
}
function show(data) {
  const {profile, releases} = data;
  if (!releases.length) throw new Error('No published runs are available.');
  get('machine').textContent = profile.cpu + ' / ' + fmt(profile.ram_gib, 'GiB');
  get('workload').textContent = profile.map + ' / ' + profile.tanks + ' scripted players per arena';
  get('measurement').textContent = profile.seconds + ' s after ' + profile.warmup + ' s warm-up';
  get('build').textContent = profile.game_build;
  get('method-profile').textContent = 'Profile ' + profile.id + ' · Seed ' + profile.seed + ' · Target ' + profile.target_hz + ' Hz';
  for (const count of profile.arenas) get('arena').add(new Option(String(count), String(count)));
  for (const [key, [label]] of Object.entries(stats)) get('metric').add(new Option(label, key));
  function render() {
    const key = get('metric').value;
    const [title, unit, direction] = stats[key];
    const records = releases.flatMap(release => {
      const run = release.runs.find(row => row.arenas === Number(get('arena').value));
      return run ? [[release, run]] : [];
    });
    get('graph-title').textContent = title;
    get('direction').textContent = direction;
    const values = records.map(([, run]) => run[key]).filter(Number.isFinite);
    const largest = Math.max(key === 'hz' ? profile.target_hz : 0, ...values, 0);
    get('maximum').textContent = fmt(largest, unit);
    get('bars').replaceChildren(...records.map(([release, run]) => {
      const row = el('div', '');
      row.className = 'bar' + (release.prerelease ? ' preview' : '');
      const track = el('div', '');
      track.className = 'track';
      const fill = el('span', '');
      fill.style.setProperty('--width', ((largest && Number.isFinite(run[key])) ? run[key] / largest * 100 : 0) + '%');
      track.append(fill);
      const name = el('span', release.tag);
      name.className = 'bar-name';
      const value = el('span', fmt(run[key], unit));
      value.className = 'bar-value';
      row.append(name, track, value);
      return row;
    }));
    get('records-body').replaceChildren(...records.map(([release, run]) => {
      const row = el('tr', '');
      const version = el('th', '');
      version.scope = 'row';
      version.append(link(release.tag, 'https://github.com/unisequence/Solbreaker/releases/tag/' + release.tag));
      const commit = link(release.commit.slice(0, 8), 'https://github.com/unisequence/Solbreaker/commit/' + release.commit);
      commit.className = 'commit';
      version.append(commit);
      row.append(version, ...[release.measured, fmt(run.hz), fmt(run.input_ms, 'ms'), fmt(run.tick_ms, 'ms'),
        fmt(run.cpu), fmt(run.ram_mib, 'MiB'), fmt(run.drops), labels[run.verdict] || run.verdict].map(value => el('td', value)));
      const files = el('td', '');
      files.append(link('HTML', 'data/' + release.tag + '.html'), ' · ', link('JSON', 'data/' + release.tag + '.json'));
      row.append(files);
      return row;
    }));
  }
  get('arena').addEventListener('change', render);
  get('metric').addEventListener('change', render);
  render();
  get('message').hidden = true;
  get('dashboard').hidden = false;
}
fetch('data/index.json').then(response => {
  if (!response.ok) throw new Error('Could not load the benchmark index.');
  return response.json();
}).then(show).catch(error => { get('message').textContent = error.message; });
