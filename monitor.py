"""Sampling configuration, packet parsing and quality statistics (stdlib only)."""
from dataclasses import dataclass
import json
import logging
import math
import os
import re
import subprocess

LOG = logging.getLogger('ping_logger.probe')


@dataclass(frozen=True)
class Settings:
    interval: int = 60
    packets: int = 1
    workers: int = 4
    retention_days: int = 7
    enable_cleanup: bool = False

    def __post_init__(self):
        for name, choices in [('interval', (10, 30, 60)), ('packets', (1, 3, 5)), ('retention_days', (7, 30))]:
            if type(getattr(self, name)) is not int or getattr(self, name) not in choices:
                raise ValueError(f'{name} must be one of {choices}')
        if type(self.workers) is not int or not 1 <= self.workers <= 20:
            raise ValueError('workers must be between 1 and 20')
        if type(self.enable_cleanup) is not bool:
            raise ValueError('enable_cleanup must be boolean')


@dataclass
class Result:
    ms: float | None
    status: str
    reason: str
    sent: int
    received: int
    rtts: list

    @property
    def lost(self):
        return self.sent - self.received


def parse_result(code, output, packets=1):
    lower = output.lower()
    reasons = [('operation not permitted', 'permission'), ('permission denied', 'permission'),
               ('not permitted', 'permission'), ('network is unreachable', 'no_route'),
               ('no route to host', 'no_route')]
    reason = next((reason for text, reason in reasons if text in lower), None)
    if reason:
        return Result(None, 'error', reason, 0, 0, [])
    rtts = [float(m[1]) for m in re.findall(r'time([=<])\s*([\d.]+)\s*ms', output)]
    rtts = [r for r in rtts if math.isfinite(r) and r >= 0]
    summary = re.search(r'(\d+) packets transmitted,\s*(\d+) (?:packets )?received', lower)
    sent, received = (map(int, summary.groups()) if summary else (packets, len(rtts)))
    # Never manufacture RTTs from aggregate output or accept inconsistent packet counts.
    if received > sent or received != len(rtts) or sent > packets or code not in (0, 1):
        return Result(None, 'error', 'system_error', 0, 0, [])
    if sent == 0:
        return Result(None, 'error', 'system_error', 0, 0, [])
    reason = 'unreachable' if 'unreachable' in lower else 'timeout'
    if received:
        return Result(sum(rtts)/received, 'ok', 'success' if sent == received else reason, sent, received, rtts)
    return Result(None, 'loss', reason, sent, 0, [])


def probe(address, packets=1):
    try:
        # Do not combine count with -w: iputils may keep transmitting until
        # replies reach the count, producing extra packets on paths slower than -i.
        result = subprocess.run(['ping', '-n', '-c', str(packets), '-i', '0.2', '-W', '2', address],
                                capture_output=True, text=True, timeout=6,
                                env=dict(os.environ, LC_ALL='C'))
        outcome = parse_result(result.returncode, result.stdout + result.stderr, packets)
    except subprocess.TimeoutExpired:
        # The process safeguard expired: this is a local execution fault.
        LOG.warning('probe_process_timeout address=%s deadline=6', address)
        return Result(None, 'error', 'process_timeout', 0, 0, [])
    except FileNotFoundError:
        LOG.error('probe_command_missing address=%s', address)
        return Result(None, 'error', 'command_missing', 0, 0, [])
    except PermissionError:
        LOG.error('probe_permission address=%s', address)
        return Result(None, 'error', 'permission', 0, 0, [])
    except OSError:
        LOG.exception('probe_system_error address=%s', address)
        return Result(None, 'error', 'system_error', 0, 0, [])
    if outcome.status == 'error':
        LOG.warning('probe_error address=%s reason=%s', address, outcome.reason)
    else:
        LOG.debug('probe_result address=%s reason=%s received=%s sent=%s', address, outcome.reason, outcome.received, outcome.sent)
    return outcome


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    pos = (len(values)-1)*p
    lo, hi = math.floor(pos), math.ceil(pos)
    return values[lo] + (values[hi]-values[lo])*(pos-lo)


def statistics(rows):
    """Rows: ts,ms,status,reason,sent,received,min_ms,max_ms,rtts,interval."""
    rtts, sent, received, errors = [], 0, 0, 0
    last_success = last_failure = None
    consecutive = 0
    previous = None
    for row in rows:
        ts, ms, status, reason, tx, rx, _, _, raw, interval = row
        if previous and ts-previous[0] > max(interval, previous[1])*1.5:
            consecutive = 0
        previous = (ts, interval)
        if status == 'error':
            errors += 1
            consecutive = 0
            continue
        sent += tx
        received += rx
        rtts.extend(json.loads(raw))
        if rx:
            last_success = ts
            consecutive = 0
        elif tx:
            consecutive += 1
        if tx > rx:
            last_failure = ts
    return {'samples': len(rows), 'successful_packets': received, 'lost_packets': sent-received,
            'probe_errors': errors, 'min_ms': min(rtts) if rtts else None,
            'avg_ms': sum(rtts)/len(rtts) if rtts else None, 'max_ms': max(rtts) if rtts else None,
            'p50_ms': percentile(rtts, .50), 'p95_ms': percentile(rtts, .95), 'p99_ms': percentile(rtts, .99),
            'loss_pct': 100*(sent-received)/sent if sent else None,
            'consecutive_loss_cycles': consecutive, 'last_success': last_success, 'last_failure': last_failure,
            'percentile_method': 'linear interpolation over successful packet RTTs'}
