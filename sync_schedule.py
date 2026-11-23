import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'dist/schedule-data.json'
SEED = ROOT / 'schedule-data.json'
API = 'https://qr.agni-rt.ru/schedule/api/'
MOSCOW = timezone(timedelta(hours=3))
FIELDS = ('date', 'start_time', 'end_time', 'discipline', 'lesson_type',
          'teacher', 'room', 'address', 'note', 'pair_number')


class DownloadError(Exception):
    def __init__(self, status):
        super().__init__(f'HTTP {status}' if status else 'соединение недоступно')
        self.status = status


def download_json(url):
    response = subprocess.run(
        ['curl', '--silent', '--show-error', '--max-time', '12',
         '--max-filesize', '10000000', '--proto', '=https',
         '--header', 'Accept: application/json', '--header', 'Cache-Control: no-cache',
         '--write-out', '\n%{response_code}', url],
        capture_output=True, check=False,
    )
    body, _, status = response.stdout.rpartition(b'\n')
    status = int(status) if status.isdigit() else 0
    if response.returncode or status != 200:
        raise DownloadError(status if status != 200 else 0)
    return json.loads(body)


def load_previous(pages_url):
    local = DATA if DATA.exists() else SEED
    if not pages_url:
        return json.loads(local.read_text(encoding='utf-8')), local == SEED
    url = urlsplit(pages_url)
    if url.scheme != 'https' or not url.netloc or url.query or url.fragment:
        raise ValueError('Pages URL must be an HTTPS origin with an optional base path')
    try:
        previous = download_json(f'{pages_url.rstrip("/")}/schedule-data.json?run={time.time_ns()}')
    except DownloadError as error:
        if error.status != 404:
            raise
        return json.loads(SEED.read_text(encoding='utf-8')), True
    if not isinstance(previous.get('weeks'), dict) or not isinstance(previous.get('groups'), list):
        raise ValueError('Invalid schedule-data.json on the published site')
    return previous, False


def is_group(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 48


def timestamp(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError('Timezone required')
    return moment


def fetch_group(group, first, next_week, last):
    query = urlencode({'group': group, 'from': first, 'to': last})
    try:
        result = download_json(API + 'items/?' + query)
        if (result.get('ok') is not True or result.get('group') != group
                or result.get('date_from') != first or result.get('date_to') != last
                or not isinstance(result.get('items'), list)):
            raise ValueError('Unexpected schedule response')
        updated = result['updated_at']
        timestamp(updated)
        slots = {first: [], next_week: []}
        for item in result['items']:
            iso = date.fromisoformat(item['date']).isoformat()
            if not first <= iso <= last:
                raise ValueError('Lesson outside the requested range')
            target = first if iso < next_week else next_week
            slots[target].append({
                key: item[key] for key in FIELDS if isinstance(item.get(key), str) and item[key]
            })
        return group, {week: {'updated_at': updated, 'stale': bool(result.get('stale')), 'items': items}
                       for week, items in slots.items()}
    except (DownloadError, ValueError, TypeError, AttributeError, KeyError):
        return group, None
    finally:
        time.sleep(0.6)


def merge(old, new):
    if old and (new is None or (new['stale'] and timestamp(old['updated_at']) >= timestamp(new['updated_at']))):
        return {**old, 'stale': True}
    return new


def write(data):
    DATA.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    temporary.replace(DATA)


def main():
    previous, initial = load_previous(os.environ.get('VSHN_PAGES_URL', ''))
    groups = previous['groups']
    try:
        directory = download_json(API + 'groups/')
        if directory.get('ok') and not directory.get('stale') and isinstance(directory.get('groups'), list):
            published = list(dict.fromkeys(group for group in directory['groups'] if is_group(group)))
            if published:
                groups = published
    except (DownloadError, ValueError, AttributeError):
        print('::warning::Список групп ВШН недоступен. Используется предыдущий список.')

    today = datetime.now(MOSCOW).date()
    monday = today - timedelta(days=today.weekday())
    past, first, next_week, last = ((monday + timedelta(days=n)).isoformat() for n in (-7, 0, 7, 13))
    old_weeks = previous['weeks']
    weeks = {past: old_weeks.get(past, {}), first: {}, next_week: {}}
    success = 0

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = pool.map(lambda group: fetch_group(group, first, next_week, last), groups)
        for group, results_by_week in results:
            if results_by_week is not None:
                success += 1
            for week in (first, next_week):
                old = old_weeks.get(week, {}).get(group)
                entry = merge(old, results_by_week[week] if results_by_week else None)
                if entry is not None:
                    weeks[week][group] = entry

    if success == 0:
        if not initial:
            raise SystemExit('Источник ВШН не вернул расписание ни для одной группы. Публикация пропущена.')
        print('::warning::Источник ВШН недоступен. При первой публикации используется стартовая копия.')
        write(previous)
        return
    if success < len(groups):
        print(f'::warning::ВШН ответила для {success} из {len(groups)} групп. Для остальных сохранены доступные копии.')
    write({'groups': groups, 'weeks': {week: entries for week, entries in weeks.items() if entries}})
    print(f'ВШН ответила для {success} из {len(groups)} групп. Диапазон запросов {first} — {last}.')


if __name__ == '__main__':
    main()
