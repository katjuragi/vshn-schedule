import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from notice_feed import write_feed

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'dist/schedule-data.json'
SEED = ROOT / 'schedule-data.json'
API = 'https://qr.agni-rt.ru/schedule/api/'
MOSCOW = timezone(timedelta(hours=3))
FIELDS = ('date', 'start_time', 'end_time', 'discipline', 'lesson_type',
          'teacher', 'room', 'address', 'note', 'pair_number')


class DownloadError(Exception):
    def __init__(self, status, detail=''):
        super().__init__(f'HTTP {status}' if status else detail or 'соединение недоступно')
        self.status = status


def download_json(url):
    response = subprocess.run(
        ['curl', '--silent', '--show-error', '--connect-timeout', '4', '--max-time', '10',
         '--max-filesize', '10000000', '--proto', '=https',
         '--header', 'Accept: application/json', '--header', 'Cache-Control: no-cache',
         '--write-out', '\n%{response_code}', url],
        capture_output=True, check=False,
    )
    body, _, status = response.stdout.rpartition(b'\n')
    status = int(status) if status.isdigit() else 0
    if response.returncode or status != 200:
        raise DownloadError(status if status != 200 else 0, response.stderr.decode(errors='replace').strip())
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


def fetch_group(group, week_starts, last):
    first = week_starts[0]
    query = urlencode({'group': group, 'from': first, 'to': last})
    try:
        result = download_json(API + 'items/?' + query)
        if (result.get('ok') is not True or result.get('group') != group
                or result.get('date_from') != first or result.get('date_to') != last
                or not isinstance(result.get('items'), list)):
            raise ValueError('Unexpected schedule response')
        updated = result['updated_at']
        timestamp(updated)
        slots = {week: [] for week in week_starts}
        for item in result['items']:
            day = date.fromisoformat(item['date'])
            iso = day.isoformat()
            if not first <= iso <= last:
                raise ValueError('Lesson outside the requested range')
            target = (day - timedelta(days=day.weekday())).isoformat()
            slots[target].append({
                key: item[key] for key in FIELDS if isinstance(item.get(key), str) and item[key]
            })
        return group, {week: {'updated_at': updated, 'stale': bool(result.get('stale')), 'items': items}
                       for week, items in slots.items()}, None
    except (DownloadError, ValueError, TypeError, AttributeError, KeyError) as error:
        return group, None, str(error)[:140]
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
    pages_url = os.environ.get('VSHN_PAGES_URL', '')
    previous, initial = load_previous(pages_url)
    prior_feed = {}
    if pages_url and not initial:
        try:
            prior_feed = download_json(f'{pages_url.rstrip("/")}/notice-feed.json?run={time.time_ns()}')
        except (DownloadError, ValueError):
            pass
    groups = previous['groups']
    try:
        directory = download_json(API + 'groups/')
        if directory.get('ok') and not directory.get('stale') and isinstance(directory.get('groups'), list):
            published = list(dict.fromkeys(group for group in directory['groups'] if is_group(group)))
            if published:
                groups = published
    except (DownloadError, ValueError, AttributeError) as error:
        print(f'::warning::Список групп ВШН недоступен. Используется предыдущий список. {error}', flush=True)

    today = datetime.now(MOSCOW).date()
    monday = today - timedelta(days=today.weekday())
    past = (monday - timedelta(days=7)).isoformat()
    week_starts = [(monday + timedelta(weeks=n)).isoformat() for n in range(6)]
    old_weeks = previous['weeks']
    weeks = {past: old_weeks.get(past, {})}
    successful = 0
    errors = set()
    deadline = time.monotonic() + 210

    for week in week_starts:
        weeks[week] = {
            group: {**entry, 'stale': True}
            for group, entry in old_weeks.get(week, {}).items() if group in groups
        }

    with ThreadPoolExecutor(max_workers=3) as pool:
        # ВШН принимает не более 31 дня за запрос.
        for window in (week_starts[:4], week_starts[4:]):
            last = (date.fromisoformat(window[-1]) + timedelta(days=6)).isoformat()
            success = 0
            for offset in range(0, len(groups), 12):
                batch = groups[offset:offset + 12]
                for group, results_by_week, error in pool.map(
                        lambda group: fetch_group(group, window, last), batch):
                    if results_by_week is None:
                        errors.add(error)
                        continue
                    success += 1
                    for week, entry in results_by_week.items():
                        old = old_weeks.get(week, {}).get(group)
                        weeks[week][group] = merge(old, entry)
                if (offset == 0 and success == 0) or time.monotonic() >= deadline:
                    break
            successful += success
            print(f'ВШН ответила для {success} из {len(groups)} групп за {window[0]} — {last}.', flush=True)
            if time.monotonic() >= deadline:
                break
    if errors:
        print('::warning::Ошибки источника: ' + '; '.join(sorted(errors)), flush=True)

    if successful == 0:
        if not initial:
            raise SystemExit('Источник ВШН не вернул расписание ни для одной группы. Публикация пропущена.')
        print('::warning::Источник ВШН недоступен. При первой публикации используется стартовая копия.')
        write(previous)
        write_feed(DATA.with_name('notice-feed.json'), previous, previous, {}, datetime.now(MOSCOW), True)
        return
    if successful < len(groups) * 2:
        print('::warning::По некоторым группам или неделям ВШН не ответила. Сохранены доступные копии.')
    published = {'groups': groups, 'weeks': {week: entries for week, entries in weeks.items() if entries}}
    write(published)
    write_feed(DATA.with_name('notice-feed.json'), previous, published, prior_feed, datetime.now(MOSCOW), initial)


if __name__ == '__main__':
    main()
