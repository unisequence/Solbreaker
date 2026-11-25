"""Build and submit the public summary of a completed local benchmark."""
import json
import math
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DISCOVERY_URL = 'https://unisequence.github.io/Solbreaker/data/community-api.json'
MAX_REPORT = 64 << 20
MAX_RESPONSE = 4096

BAD_REPORT = 'Этот JSON не содержит завершённого бенчмарка.'
NO_SERVICE = 'Таблица сообщества пока не подключена.'
SUBMIT_FAILED = 'Не удалось отправить результат.'
RATE_LIMITED = 'Слишком много отправок. Попробуй через минуту.'


class SubmissionError(ValueError):
    pass


def metric(value, scale=1):
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return round(value / scale, 3)
    return None


def combined(run, primary, secondary, single_process):
    first, second = metric(run.get(primary)), metric(run.get(secondary))
    if first is None:
        return None
    if single_process:
        return first
    return first + second if second is not None else None


def public_summary(path):
    try:
        with Path(path).open('rb') as file:
            data = file.read(MAX_REPORT + 1)
        if len(data) > MAX_REPORT:
            raise SubmissionError(BAD_REPORT)
        report = json.loads(data)
        if report['schema'] != 1 or report['complete'] is not True or not 1 <= len(report['runs']) <= 32:
            raise SubmissionError(BAD_REPORT)
        host = report['host']
        engine = report['mode'].split('_', 1)[0]
        runs = []
        for run in report['runs']:
            arenas = run.get('arenas', [])
            if not arenas or run.get('verdict') == 'error':
                continue
            rates = [metric(arena.get('steps_per_second')) for arena in arenas]
            if None in rates:
                continue
            if len({arena['vehicles'] for arena in arenas}) != 1:
                continue
            inputs = [metric(arena.get('input_queue', {}).get('p99_ms')) for arena in arenas]
            rss = combined(run, 'process_rss_bytes', 'arena_worker_rss_bytes_sum', engine == 'in-process')
            drops = combined(run, 'server_udp_receive_drops', 'peer_udp_receive_drops', False)
            runs.append(dict(map=run['map'], scenario=run['scenario'], arenas=len(arenas),
                             tanks=arenas[0]['vehicles'], seconds=report['requested_seconds'],
                             hz=min(rates), input_ms=max(inputs) if None not in inputs else None,
                             tick_ms=metric(run.get('tick_work', {}).get('p99_ms')),
                             cpu_cores=metric(combined(run, 'cpu_cores_avg', 'arena_worker_cpu_cores_avg',
                                                       engine == 'in-process')),
                             rss_mib=metric(rss, 1 << 20), udp_drops=int(drops) if drops is not None else None,
                             verdict=run['verdict']))
        if not runs:
            raise SubmissionError(BAD_REPORT)
        return dict(schema=1, version=report['version'], game_build=report['game_build'],
                    cpu=host['cpu'].strip(), os=host['os'], logical_cpus=host['logical_cpus'],
                    ram_gib=metric(host.get('physical_memory_bytes'), 1 << 30), engine=engine,
                    backend=report['dynamic_backend'], input_pattern=report['input_pattern'],
                    warmup=report['warmup_seconds'], runs=runs)
    except (KeyError, TypeError, AttributeError, OSError, UnicodeError, json.JSONDecodeError,
            OverflowError, RecursionError) as error:
        raise SubmissionError(BAD_REPORT) from error


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def request_json(request):
    with build_opener(NoRedirect()).open(request, timeout=12) as response:
        body = response.read(MAX_RESPONSE + 1)
        if len(body) > MAX_RESPONSE:
            raise SubmissionError(SUBMIT_FAILED)
        return json.loads(body)


def service_url():
    development = os.environ.get('SOLBREAKER_BENCHMARK_API_URL')
    if development:
        value = development
    else:
        try:
            value = request_json(DISCOVERY_URL)['base_url']
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise SubmissionError(NO_SERVICE) from error
    try:
        url = urlsplit(value)
        loopback = development and url.scheme == 'http' and url.hostname == '127.0.0.1'
        if ((url.scheme != 'https' and not loopback) or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment or url.path not in ('', '/')):
            raise ValueError('invalid community origin')
        return value.rstrip('/') + '/v1/results'
    except (TypeError, ValueError) as error:
        raise SubmissionError(NO_SERVICE) from error


def submit(summary):
    target = service_url()
    data = json.dumps(summary, ensure_ascii=False, allow_nan=False).encode()
    if len(data) > 32 << 10:
        raise SubmissionError(BAD_REPORT)
    try:
        result = request_json(Request(target, data=data, headers={'Content-Type': 'application/json'}))
        if not re.fullmatch(r'[a-f0-9]{64}', result['id']) or type(result['duplicate']) is not bool:
            raise SubmissionError(SUBMIT_FAILED)
        return result['duplicate']
    except HTTPError as error:
        raise SubmissionError(RATE_LIMITED if error.code == 429 else SUBMIT_FAILED) from error
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise SubmissionError(SUBMIT_FAILED) from error
