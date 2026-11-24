"""Produce the small public feed used by the Telegram notification worker."""
from collections import defaultdict
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

PROJECTION = ('date', 'start_time', 'end_time', 'discipline', 'room', 'address', 'lesson_type', 'teacher')
RETENTION = 6 * 3600


def notice_rows(entry, today):
    return {tuple(item.get(key, '') for key in PROJECTION) for item in entry['items']
            if item.get('date', '') >= today}


def describe(row):
    day, start, _, title, room, address, kind, teacher = row
    when = date.fromisoformat(day).strftime('%d.%m')
    where = ', '.join(part for part in (room, address) if part)
    return ' · '.join(part for part in (f'{when} {start}'.strip(), title, where, kind, teacher) if part)


def changes_for_group(old, new, group, today):
    removed, added = set(), set()
    for week, groups in new['weeks'].items():
        before = old['weeks'].get(week, {}).get(group)
        after = groups.get(group)
        if not before or not after or after.get('stale'):
            continue
        previous, current = notice_rows(before, today), notice_rows(after, today)
        removed.update(previous - current)
        added.update(current - previous)
    return sorted(removed), sorted(added)


def next_first_lessons(data, now):
    today = now.date()
    options = {}
    for offset in (0, 1):
        day = today + timedelta(days=offset)
        weekday = (day - timedelta(days=day.weekday())).isoformat()
        iso = day.isoformat()
        for group, entry in data['weeks'].get(weekday, {}).items():
            if group not in data['groups']:
                continue
            timed = [(start, item) for item in entry['items'] if item.get('date') == iso
                     if (start := parse_start(item.get('start_time', '')))]
            if not timed:
                continue
            first = min(start for start, _ in timed)
            at = int(datetime.combine(day, first, tzinfo=now.tzinfo).timestamp())
            details = sorted({describe(tuple(item.get(key, '') for key in PROJECTION))
                              for start, item in timed if start == first})
            text = 'Доброе утро! Первая пара группы ' + group + ' сегодня:\n' + '\n'.join(details)
            options.setdefault(iso, {})[group] = [at, text[:2500]]
    return options


def parse_start(value):
    try:
        return datetime.strptime(value, '%H:%M').time()
    except ValueError:
        return None


def create_feed(old, new, prior, now, initial=False):
    epoch = int(now.timestamp())
    events = defaultdict(list)
    active_groups = set(new['groups'])
    for group, rows in prior.get('changes', {}).items():
        recent = [row for row in rows if row[1] > epoch - RETENTION]
        if group in active_groups and recent:
            events[group].extend(recent)
    if not initial:
        for group in new['groups']:
            removed, added = changes_for_group(old, new, group, now.date().isoformat())
            if not removed and not added:
                continue
            lines = [f'Убрали: {describe(row)}' for row in removed]
            lines.extend(f'Добавили: {describe(row)}' for row in added)
            payload = json.dumps([epoch, removed, added], ensure_ascii=False).encode()
            identifier = sha256(payload).hexdigest()[:24]
            text = f'Изменилось расписание группы {group}:\n' + '\n'.join(lines[:8])
            if len(lines) > 8:
                text += f'\nИ ещё изменений: {len(lines) - 8}.'
            events[group].append([identifier, epoch, text[:2450]])
    return {'groups': new['groups'], 'first': next_first_lessons(new, now), 'changes': dict(events)}


def write_feed(path: Path, old, new, prior, now, initial=False):
    feed = create_feed(old, new, prior, now, initial)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(feed, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    temporary.replace(path)
