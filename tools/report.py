"""Render benchmark results as a standalone page using the standard library."""
from html import escape
import json
from math import isfinite
from pathlib import Path
import re

TEMPLATE = Path(__file__).with_name('assets') / 'benchmark-report.html'
VERDICTS = {
    'ok': ('Passed', 'ok'),
    'stalls': ('Timing stalls', 'warn'),
    'overloaded': ('Overloaded', 'warn'),
    'fault': ('Server fault', 'bad'),
    'error': ('Run failed', 'bad'),
    'packet_drops': ('Packet drops', 'warn'),
}


def html_output(json_path):
    path = Path(json_path)
    return path.with_suffix('.html') if path.suffix.lower() != '.html' else path.with_name(path.name + '.report.html')


def number(value, digits=2):
    if type(value) is int:
        return f'{value:,}'
    if type(value) is float and isfinite(value):
        return f'{value:,.{digits}f}'.rstrip('0').rstrip('.') if digits else f'{value:,.0f}'
    return '—'


def mib(value):
    return number(value / 2**20, 1) if type(value) in (int, float) else '—'


def text(value):
    return escape(str(value)) if value is not None else '—'


def metric(label, value, note=''):
    return (f'<div><dt>{text(label)}</dt><dd>{text(value)}</dd>'
            + (f'<p>{text(note)}</p>' if note else '') + '</div>')


def badge(verdict):
    label, style = VERDICTS.get(verdict, ('Unknown', 'unknown'))
    return f'<span class="status {style}">{label}</span>'


def run_verdict(run):
    value = run.get('verdict')
    if value == 'ok' and any((run.get(key) or 0) > 0 for key in
                             ('server_udp_receive_drops', 'peer_udp_receive_drops')):
        return 'packet_drops'
    return value


def summary(report, runs):
    verdicts = [run_verdict(run) for run in runs]
    if not report.get('complete') or not runs:
        label, style = 'Incomplete', 'unknown'
    elif any(value in ('error', 'fault') for value in verdicts):
        label, style = 'Failed', 'bad'
    elif any(value != 'ok' for value in verdicts):
        label, style = 'Review needed', 'warn'
    else:
        label, style = 'Passed', 'ok'
    passed = verdicts.count('ok')
    return f'<span class="status {style}">{label}</span><span>{passed} of {len(runs)} runs passed</span>'


def timing(info, field, key='p99_ms'):
    return info.get(field, {}).get(key)


def arena_table(arenas):
    if not arenas:
        return '<p class="empty">No arena measurements were recorded.</p>'
    columns = ('Arena', 'Players', 'Bots', 'Tanks', 'Hz', 'Dropped s', 'Input p99 ms',
               'Motion gap p99 ms', 'Worker step p99 ms', 'Controls queued / sent',
               'Queue overflows', 'Worker CPU cores', 'Build s')
    rows = []
    for index, arena in enumerate(arenas, 1):
        cells = [number(arena.get(key)) for key in
                 ('players', 'bots', 'vehicles', 'steps_per_second', 'dropped_seconds')]
        cells.extend(number(timing(arena, key)) for key in ('input_queue', 'motion_gap', 'owner_step_roundtrip'))
        cells.extend((f"{number(arena.get('controls_queued'))} / {number(arena.get('controls_sent'))}",
                      number(arena.get('input_overflows')), number(arena.get('worker_cpu_cores_avg')),
                      number(arena.get('build_seconds'))))
        rows.append(f'<tr><th scope="row">{index:02d}</th>' +
                    ''.join(f'<td>{value}</td>' for value in cells) + '</tr>')
    return ('<div class="table-wrap" tabindex="0" role="region" aria-label="Arena statistics">'
            '<table><thead><tr>' + ''.join(f'<th scope="col">{heading}</th>' for heading in columns)
            + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')


def run_section(run, index):
    arenas = run.get('arenas', [])
    rates = [arena['steps_per_second'] for arena in arenas if arena.get('steps_per_second') is not None]
    health = ''.join((
        metric('Lowest arena frequency', number(min(rates, default=None)), 'Hz'),
        metric('Tick work p99', number(timing(run, 'tick_work')), 'ms per server tick'),
        metric('Server CPU average', number(run.get('cpu_cores_avg')), 'logical cores'),
        metric('Server RAM at finish', mib(run.get('process_rss_bytes')), 'MiB resident'),
    ))
    network_values = (
        ('Server receive drops', 'server_udp_receive_drops'),
        ('Client receive drops', 'peer_udp_receive_drops'),
        ('Reliable retransmits', 'reliable_retransmissions'),
        ('Server → clients', 'loopback_packets'),
        ('Clients → server', 'loopback_packets_sent'),
    )
    network = ''.join(metric(label, number(run.get(key)),
                             'Unavailable' if run.get(key) is None else 'packets')
                      for label, key in network_values)
    network += metric('Smoothed RTT p99', number(timing(run, 'server_rtt_at_end')), 'ms at finish')
    fault_rows = ''.join(f'<li>{text(key)} <strong>{number(value)}</strong></li>'
                         for key, value in run.get('faults', {}).items())
    fault_summary = (f'<ul class="faults">{fault_rows}</ul>' if fault_rows else '')
    error = f'<p class="empty">Run error: {text(run["error"])}</p>' if run.get('error') else ''
    return f'''
<section class="run" data-run="{index}" aria-labelledby="run-{index}">
  <div class="run-heading">
    <div><h2 id="run-{index}">{text(run.get('map'))} / {text(run.get('scenario'))}</h2>
    <p>{number(run.get('concurrent_arenas'))} arenas · {number(run.get('seconds'))} s measured · Seed {number(run.get('seed'))}</p></div>
    {badge(run_verdict(run))}
  </div>
  {error}
  <dl class="metrics">{health}</dl>
  <div class="chart-anchor"></div>
  <h3>Per-arena statistics</h3>
  {arena_table(arenas)}
  <h3>Loopback network</h3>
  <dl class="metrics network">{network}</dl>
  <p class="muted">Receive drops are socket counters where supported. Retransmits and packet totals measure separate events.</p>
  {fault_summary}
</section>'''


def build_html(report):
    if (not isinstance(report, dict) or report.get('schema') != 1
            or not isinstance(report.get('runs'), list)
            or any(not isinstance(run, dict) for run in report['runs'])):
        raise ValueError('Expected a Solbreaker benchmark JSON report (schema 1)')
    runs = report['runs']
    host = report.get('host', {})
    options = ''.join(f'<option value="{i}">{i + 1:02d} · {text(run.get("map"))} / '
                      f'{text(run.get("scenario"))} · {number(run.get("concurrent_arenas"))} arenas</option>'
                      for i, run in enumerate(runs))
    graphics = [{
        'arenaCount': len(run.get('arenas', [])),
        'rows': run.get('timeline') or run.get('windows', []),
        'legacy': not run.get('timeline'),
    } for run in runs]
    # JSON script data must not terminate its script element, even with a hand-edited JSON input.
    data = json.dumps({'target': report.get('target_step_hz'), 'runs': graphics},
                      ensure_ascii=True, allow_nan=False).replace('<', r'\u003c')
    values = {
        'VERSION': text(report.get('version')), 'BUILD': text(report.get('game_build')),
        'SUMMARY': summary(report, runs), 'OPTIONS': options,
        'HOST': ''.join((metric('Processor', host.get('cpu') or '—'),
                        metric('Host RAM', mib(host.get('physical_memory_bytes')), 'MiB'),
                        metric('Target simulation', number(report.get('target_step_hz')), 'Hz'),
                        metric('Run configuration', report.get('mode', '—').split('_', 1)[0],
                               f'{number(report.get("requested_seconds"))} s per run'))),
        'RUNS': ''.join(run_section(run, i) for i, run in enumerate(runs))
        or '<p class="empty">No runs were recorded.</p>',
        'DATA': data,
    }
    template = TEMPLATE.read_text(encoding='utf-8')
    return re.sub(r'@@([A-Z]+)@@', lambda match: values[match[1]], template)


def write_html(report, destination):
    content = '\n'.join(line.rstrip() for line in build_html(report).splitlines()) + '\n'
    with Path(destination).open('x', encoding='utf-8', newline='\n') as file:
        file.write(content)


def convert(json_path, destination=None):
    source = Path(json_path)
    with source.open(encoding='utf-8') as file:
        report = json.load(file)
    output = destination if destination is not None else html_output(source)
    try:
        write_html(report, output)
    except (AttributeError, TypeError, OverflowError) as error:
        raise ValueError('Malformed benchmark JSON report') from error
    return output
