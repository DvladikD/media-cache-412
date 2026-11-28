#!/usr/bin/env python3
"""Сборщик автообновляемого медиа-плейлиста.

Что делает:
  1. скачивает публичные M3U-источники из config/sources.txt;
  2. оставляет каналы России/СНГ (или каналы с совпавшим EPG);
  3. автоматически сопоставляет каналы с XMLTV (iptvx.one) по tvg-id/имени;
  4. проверяет потоки через ffprobe и оставляет до N рабочих потоков на канал
     (плеер при включённом folding сам переключится на следующий);
  5. сортирует каналы по категориям и даёт им официальные имена;
  6. публикует playlist.m3u и урезанный epg.xml.gz в output/.

Запуск в GitHub Actions: python scripts/build.py
Локальная проверка без ffprobe: python scripts/build.py --no-probe --epg-file <путь к epg.xml.gz>
"""

from __future__ import annotations

import argparse
import csv
import difflib
import gzip
import io
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from xml.sax.saxutils import quoteattr

ROOT = Path(__file__).resolve().parent.parent
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
MARK_RE = re.compile(r'[ⓈⓉⒼⓏⓇⓎⒻⒽⓁ✓]')
QUALITY_RE = re.compile(r'\b(hd|sd|4k|uhd|fhd|hdr|hevc|\d{3,4}[ip])\b', re.IGNORECASE)
BRACKET_RE = re.compile(r'[\(\[][^\)\]]*[\)\]]')
PAREN_RE = re.compile(r'\(([^)]*)\)')
SQUARE_RE = re.compile(r'\[[^\]]*\]')
SHIFT_RE = re.compile(r'\(\s*([+-]\d{1,2})\s*\)')
QUALITY_ONLY_RE = re.compile(r'(hd|sd|4k|uhd|fhd|hdr|hevc|\d{3,4}[ip])', re.IGNORECASE)
COUNTRY_TAGS = {
    'RU', 'BY', 'UA', 'KZ', 'AM', 'AZ', 'GE', 'KG', 'MD', 'TJ', 'TM', 'UZ',
}
IGNORED_PARENS = {'архив', 'archive', 'архивная версия', 'запись', 'recap'}
BAD_CHARS_RE = re.compile(r'[^\w ]+', re.UNICODE)
SPACE_RE = re.compile(r'\s+')

DEFAULT_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
)


def log(message: str) -> None:
    print(f'[{datetime.now(timezone.utc).strftime("%H:%M:%S")}] {message}', flush=True)


def normalize(value: str) -> str:
    """Приводит название канала к виду, пригодному для сравнения с EPG."""
    text = (value or '').lower().replace('ё', 'е')
    text = MARK_RE.sub(' ', text)
    text = BRACKET_RE.sub(' ', text)
    text = QUALITY_RE.sub(' ', text)
    text = BAD_CHARS_RE.sub(' ', text)
    text = SPACE_RE.sub(' ', text).strip()
    return text


def kept_parentheses(value: str) -> list[str]:
    """Скобки, которые нужно сохранить: регион/версия, но не HD/гео/сдвиг."""
    result: list[str] = []
    for inner in PAREN_RE.findall(value or ''):
        inner = inner.strip()
        if not inner:
            continue
        if QUALITY_ONLY_RE.fullmatch(inner):
            continue
        if re.fullmatch(r'[+-]?\d{1,2}', inner):
            continue
        if inner.upper() in COUNTRY_TAGS:
            continue
        if inner.lower() in IGNORED_PARENS:
            continue
        result.append(inner)
    return result


def clean_official(value: str) -> str:
    """Читаемое имя: убирает HD/гео/сдвиг, но сохраняет регион в скобках."""
    text = (value or '').strip()
    text = MARK_RE.sub(' ', text)
    text = SHIFT_RE.sub(' ', text)
    text = SQUARE_RE.sub(' ', text)

    def strip_unneeded(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if inner in kept_parentheses(value):
            return match.group(0)
        return ' '

    text = PAREN_RE.sub(strip_unneeded, text)
    text = QUALITY_RE.sub(' ', text)
    text = SPACE_RE.sub(' ', text).strip(' -–—')
    return text


def with_source_details(official: str, source_name: str, shift: int) -> str:
    """Добавляет к каноническому имени регион и сдвиг из исходного плейлиста."""
    name = official.strip()
    for region in kept_parentheses(source_name):
        if region not in name:
            name = f'{name} ({region})'
    if '|' in source_name:
        detail = clean_official(source_name.split('|')[-1])
        if detail and detail.lower() not in name.lower() and len(detail) <= 40:
            name = f'{name} ({detail})'
    if shift and f'{shift:+d}' not in name:
        name = f'{name} ({shift:+d})'
    return name


def pick_display_name(raw_names: list[str], source_name: str) -> str:
    """Выбирает лучший display-name из XMLTV для данного канала."""
    cleaned = [clean_official(name) for name in raw_names]
    cleaned = [name for name in cleaned if name]
    if not cleaned:
        return clean_official(source_name)

    counts: dict[str, int] = {}
    for name in cleaned:
        counts[name] = counts.get(name, 0) + 1
    source_norm = normalize(source_name)
    best = max(
        cleaned,
        key=lambda name: (
            counts[name],
            difflib.SequenceMatcher(None, normalize(name), source_norm).ratio(),
            -len(name),
        ),
    )
    return best


def load_canonical_names(settings: dict) -> dict[str, str]:
    """site_id iptvx.one → чистое русское имя из iptv-org/epg."""
    url = settings.get(
        'canonical_names_url',
        'https://raw.githubusercontent.com/iptv-org/epg/master/'
        'sites/epg.iptvx.one/epg.iptvx.one.channels.xml',
    )
    try:
        root = ET.fromstring(fetch_text(url, timeout=60))
    except Exception as error:
        log(f'Не удалось загрузить справочник имён: {error}')
        return {}
    names: dict[str, str] = {}
    for element in root.iter('channel'):
        site_id = (element.get('site_id') or '').strip()
        name = clean_official(element.text or '')
        if site_id and name:
            names[site_id] = name
    log(f'Справочник официальных имён: {len(names)} записей')
    return names


def load_database_names(settings: dict) -> tuple[dict[str, str], dict[str, set[str]]]:
    """ID iptv-org → (русское имя, категории из базы каналов)."""
    url = settings.get(
        'channels_csv_url',
        'https://raw.githubusercontent.com/iptv-org/database/master/data/channels.csv',
    )
    try:
        raw = fetch_text(url, timeout=120)
    except Exception as error:
        log(f'Не удалось загрузить channels.csv: {error}')
        return {}, {}
    names: dict[str, str] = {}
    categories: dict[str, set[str]] = {}
    topic_map = settings.get('database_category_map', {})
    for row in csv.DictReader(io.StringIO(raw)):
        channel_id = (row.get('id') or '').strip()
        if not channel_id:
            continue
        alternatives = [part.strip() for part in (row.get('alt_names') or '').split(';')]
        russian = [
            part for part in alternatives
            if any('\u0400' <= char <= '\u04ff' for char in part)
        ]
        name = clean_official(russian[0] if russian else row.get('name', ''))
        if name:
            names[channel_id.lower()] = name
        raw_categories = {
            part.strip().lower() for part in (row.get('categories') or '').split(';')
        }
        topics = set()
        for raw in raw_categories:
            for key, topic in topic_map.items():
                if key.lower() == raw:
                    topics.add(topic)
                    break
        if topics:
            categories[channel_id.lower()] = topics
    log(f'Справочник iptv-org/database: {len(names)} имён, {len(categories)} категорий')
    return names, categories


def assign_category(channel: Channel, settings: dict) -> str:
    name = normalize(f'{channel.name} {channel.official_name}')
    if any(keyword in name for keyword in settings.get('dnr_keywords', [])):
        return settings.get('dnr_category', 'ДНР')

    cng_category = settings.get('cng_category', 'СНГ')
    country = channel.country.strip().upper()
    tvg_id = (channel.tvg_id or '').lower()
    epg_id = (channel.epg_id or '').lower()
    is_russian = (
        country == 'RU'
        or '.ru@' in tvg_id
        or '.ru@' in epg_id
        or any(keyword in name for keyword in settings.get('russian_name_keywords', []))
    )
    is_cis = (
        (country in settings.get('country_categories', {}) and country != 'RU')
        or any(keyword in name for keyword in settings.get('cng_name_keywords', []))
        or any(suffix in tvg_id for suffix in settings.get('cng_id_suffixes', []))
        or any(suffix in epg_id for suffix in settings.get('cng_id_suffixes', []))
    )
    if is_cis:
        return cng_category

    primary = channel.group.split(';')[0].strip()
    normalized = normalize(primary)
    mapping = settings.get('category_map', {})
    mapped = ''
    for key, value in mapping.items():
        if normalize(key) == normalized:
            mapped = value
            break
    if not mapped:
        for key, value in mapping.items():
            if normalize(key) and normalize(key) in normalized:
                mapped = value
                break

    if is_russian:
        if mapped in settings.get('topic_categories', []):
            return mapped
        for db_id in {tvg_id.split('@')[0], epg_id.split('@')[0]}:
            for topic in settings.get('_database_categories', {}).get(db_id, ()):
                if topic in settings.get('topic_categories', []):
                    return topic
        for topic in settings.get('_epg_categories', {}).get(epg_id, ()):
            if topic in settings.get('topic_categories', []):
                return topic
        for topic, keywords in settings.get('topic_name_keywords', {}).items():
            if any(keyword in name for keyword in keywords):
                return topic
        return settings.get('russian_category', 'Россия')
    if mapped:
        return mapped
    if '.ru@' in tvg_id or '.ru@' in epg_id:
        return settings.get('russian_category', 'Россия')
    return settings.get('default_category', 'Прочее')


@dataclass
class Channel:
    name: str
    tvg_name: str
    tvg_id: str
    logo: str
    group: str
    country: str
    url: str
    user_agent: str = ''
    referrer: str = ''
    source: str = ''
    norm_name: str = ''
    epg_id: str = ''
    epg_score: float = 0.0
    score: int = -1
    official_name: str = ''
    category: str = ''
    ch_number: int = 0
    tvg_shift: int = 0
    forced: bool = False
    attrs: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.norm_name = normalize(self.tvg_name or self.name)


def read_settings(path: Path) -> dict:
    with path.open('r', encoding='utf-8') as handle:
        return json.load(handle)


def channel_to_state(channel: Channel) -> dict:
    return {
        'name': channel.name,
        'tvg_name': channel.tvg_name,
        'tvg_id': channel.tvg_id,
        'logo': channel.logo,
        'group': channel.group,
        'country': channel.country,
        'url': channel.url,
        'user_agent': channel.user_agent,
        'referrer': channel.referrer,
        'source': channel.source,
        'tvg_shift': channel.tvg_shift,
        'epg_id': channel.epg_id,
        'epg_score': channel.epg_score,
        'score': channel.score,
        'alive': channel.score >= 0,
        'last_seen': datetime.now(timezone.utc).isoformat(),
    }


def load_known_streams(path: Path, max_age_days: int) -> list[Channel]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        log(f'Не удалось прочитать состояние потоков: {error}')
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    restored: list[Channel] = []
    for item in payload:
        try:
            seen = datetime.fromisoformat(item['last_seen'])
        except (KeyError, ValueError):
            continue
        if seen < cutoff:
            continue
        restored.append(
            Channel(
                name=item.get('name', ''),
                tvg_name=item.get('tvg_name', item.get('name', '')),
                tvg_id=item.get('tvg_id', ''),
                logo=item.get('logo', ''),
                group=item.get('group', ''),
                country=item.get('country', ''),
                url=item.get('url', ''),
                user_agent=item.get('user_agent', ''),
                referrer=item.get('referrer', ''),
                source='state',
                tvg_shift=int(item.get('tvg_shift', 0) or 0),
                epg_id=item.get('epg_id', ''),
                epg_score=float(item.get('epg_score', 0) or 0),
                score=int(item.get('score', -1)),
            )
        )
    log(f'Из состояния восстановлено потоков: {len(restored)}')
    return restored


def save_known_streams(path: Path, channels: list[Channel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = [channel_to_state(channel) for channel in channels]
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=1) + '\n', encoding='utf-8'
    )
    log(f'Состояние сохранено: {len(state)} потоков '
        f'({sum(1 for item in state if item["alive"])} живых)')


def fetch_text(url: str, timeout: int = 90) -> str:
    request = Request(url, headers={'User-Agent': DEFAULT_UA})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return raw.decode('utf-8-sig', errors='replace')


def fetch_binary(url: str, destination: Path, timeout: int = 180) -> None:
    request = Request(url, headers={'User-Agent': DEFAULT_UA})
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(request, timeout=timeout) as response, destination.open('wb') as out:
        while chunk := response.read(1024 * 1024):
            out.write(chunk)


def parse_m3u(text: str, source: str) -> list[Channel]:
    channels: list[Channel] = []
    pending: tuple[dict[str, str], str, list[tuple[str, str]]] | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith('#EXTINF:'):
            attrs = dict(ATTR_RE.findall(line))
            name = line.rsplit(',', 1)[-1].strip() if ',' in line else ''
            pending = (attrs, name, [])
        elif line.startswith('#EXTVLCOPT:') and pending is not None:
            key, sep, value = line[len('#EXTVLCOPT:'):].partition('=')
            if sep:
                pending[2].append((key.strip().lower(), value.strip()))
        elif line and not line.startswith('#') and pending is not None:
            attrs, name, options = pending
            name = unquote(name)
            attrs = {key: unquote(value) for key, value in attrs.items()}
            shift_match = SHIFT_RE.search(name)
            tvg_shift = int(shift_match.group(1)) if shift_match else 0
            if not tvg_shift:
                id_shift = re.search(r'-(pl|m)(\d{1,2})$', attrs.get('tvg-id', '').lower())
                if id_shift:
                    value = int(id_shift.group(2))
                    tvg_shift = value if id_shift.group(1) == 'pl' else -value
            if not -12 <= tvg_shift <= 12:
                tvg_shift = 0
            user_agent = attrs.get('http-user-agent', '')
            referrer = attrs.get('http-referrer', '')
            for key, value in options:
                if key == 'http-user-agent':
                    user_agent = value
                elif key == 'http-referrer':
                    referrer = value
            channels.append(
                Channel(
                    name=name,
                    tvg_name=attrs.get('tvg-name', name),
                    tvg_id=attrs.get('tvg-id', ''),
                    logo=attrs.get('tvg-logo', ''),
                    group=attrs.get('group-title', ''),
                    country=attrs.get('tvg-country', ''),
                    url=line,
                    user_agent=user_agent,
                    referrer=referrer,
                    source=source,
                    tvg_shift=tvg_shift,
                    attrs=attrs,
                )
            )
            pending = None
    return channels


def load_epg_channels(
    epg_path: Path,
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, str], dict[str, str]]:
    """Читает только блок <channel> в начале XMLTV (до первого <programme>)."""
    by_id: dict[str, list[str]] = {}
    raw_by_id: dict[str, list[str]] = {}
    xml_by_id: dict[str, str] = {}
    tv_attrs: dict[str, str] = {}

    with gzip.open(epg_path, 'rb') as source:
        context = ET.iterparse(source, events=('start', 'end'))
        for event, element in context:
            if event == 'start' and element.tag == 'tv':
                tv_attrs = dict(element.attrib)
            elif event == 'start' and element.tag == 'programme':
                break
            elif event == 'end' and element.tag == 'channel':
                channel_id = element.get('id', '')
                if channel_id:
                    names = []
                    raw_names = []
                    for node in element.findall('display-name'):
                        raw = (node.text or '').strip()
                        normalized = normalize(raw)
                        if normalized:
                            names.append(normalized)
                            raw_names.append(raw)
                    by_id[channel_id] = names
                    raw_by_id[channel_id] = raw_names
                    xml_by_id[channel_id] = ET.tostring(element, encoding='unicode')
                element.clear()

    log(f'EPG: прочитано {len(by_id)} каналов')
    return by_id, raw_by_id, xml_by_id, tv_attrs


def load_epg_categories(epg_path: Path, settings: dict) -> dict[str, set[str]]:
    """Категории программ из XMLTV — помогают разложить каналы по темам."""
    raw_map: dict[str, set[str]] = {}
    with gzip.open(epg_path, 'rb') as source:
        for _event, element in ET.iterparse(source, events=('end',)):
            if element.tag == 'programme':
                channel_id = element.get('channel', '')
                categories = {
                    (node.text or '').strip() for node in element.findall('category')
                }
                if channel_id and categories:
                    raw_map.setdefault(channel_id, set()).update(categories)
                element.clear()
            elif element.tag == 'channel':
                element.clear()

    mapping = settings.get('epg_category_map', {})
    result: dict[str, set[str]] = {}
    for channel_id, categories in raw_map.items():
        topics = set()
        for category in categories:
            lowered = category.lower()
            for key, topic in mapping.items():
                if key.lower() in lowered:
                    topics.add(topic)
                    break
        if topics:
            result[channel_id] = topics
    log(f'EPG-категории: {len(result)} каналов')
    return result


def build_name_index(by_id: dict[str, list[str]]) -> tuple[dict[str, list[str]], list[str]]:
    name_to_ids: dict[str, list[str]] = {}
    for channel_id, names in by_id.items():
        for name in names:
            name_to_ids.setdefault(name, []).append(channel_id)
    return name_to_ids, list(name_to_ids)


def build_token_index(name_to_ids: dict[str, list[str]]) -> dict[str, list[str]]:
    """Дешёвый индекс по словам названия: не перебираем все тысячи каналов EPG."""
    index: dict[str, set[str]] = {}
    for name, ids in name_to_ids.items():
        for token in name.split():
            if len(token) >= 3:
                index.setdefault(token, set()).update(ids)
    return {token: sorted(ids) for token, ids in index.items()}


def match_epg(
    channel: Channel,
    by_id: dict[str, list[str]],
    name_to_ids: dict[str, list[str]],
    token_index: dict[str, list[str]],
) -> tuple[str, float]:
    if channel.tvg_id and channel.tvg_id in by_id:
        return channel.tvg_id, 1.0

    candidates = [channel.norm_name, normalize(channel.name)]
    for candidate in candidates:
        if candidate and candidate in name_to_ids:
            return name_to_ids[candidate][0], 1.0

    best_id, best_score = '', 0.0
    for candidate in candidates:
        if not candidate:
            continue
        scores: dict[str, float] = {}
        for token in candidate.split():
            if len(token) < 3:
                continue
            ids = token_index.get(token, [])
            if len(ids) > 300:  # слишком общее слово — мало пользы
                continue
            for channel_id in ids:
                if channel_id in scores:
                    continue
                score = max(
                    difflib.SequenceMatcher(None, candidate, known).ratio()
                    for known in by_id[channel_id][:4]
                )
                if score >= 0.82:
                    scores[channel_id] = score
        if scores:
            channel_id = max(scores, key=scores.get)
            if scores[channel_id] > best_score:
                best_id, best_score = channel_id, scores[channel_id]
    return best_id, best_score


def is_blocked(channel: Channel, settings: dict) -> bool:
    haystack = f'{channel.name} {channel.group}'.lower()
    for word in settings.get('exclude_words', []):
        if word.lower() in haystack:
            return True
    return not channel.url.lower().startswith(('http://', 'https://'))


def is_priority(channel: Channel, wanted: set[str]) -> bool:
    return channel.norm_name in wanted or normalize(channel.name) in wanted


def in_focus(channel: Channel, settings: dict) -> bool:
    codes = {str(code).upper() for code in settings.get('countries', [])}
    if channel.country.strip().upper() in codes:
        return True
    tvg_id = channel.tvg_id.lower()
    if any(suffix in tvg_id for suffix in settings.get('tvg_id_suffixes', [])):
        return True
    group = normalize(channel.group)
    name = normalize(channel.name)
    for word in settings.get('country_words', []):
        normalized = normalize(word)
        if not normalized:
            continue
        if normalized in group:
            return True
        # Слова вроде «Georgia» дают ложные срабатывания в названиях,
        # поэтому в имени канала ищем только кириллические варианты.
        cyrillic = any('\u0400' <= char <= '\u04ff' for char in normalized)
        if cyrillic and normalized in name:
            return True
    return False


def probe(channel: Channel, settings: dict) -> int:
    """Возвращает оценку потока (чем больше, тем лучше) или -1, если мёртв."""
    command = ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', '-show_format']
    headers = [f'User-Agent: {channel.user_agent or settings.get("user_agent", DEFAULT_UA)}']
    if channel.referrer:
        headers.append(f'Referer: {channel.referrer}')
    command += ['-headers', '\r\n'.join(headers) + '\r\n', channel.url]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=int(settings.get('probe_timeout_seconds', 12)),
        )
    except (subprocess.TimeoutExpired, OSError):
        return -1
    if result.returncode != 0:
        return -1
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return -1

    videos = [stream for stream in info.get('streams', []) if stream.get('codec_type') == 'video']
    if not videos:
        return -1
    width = int(videos[0].get('width') or 0)
    height = int(videos[0].get('height') or 0)
    score = width * height or 1
    if channel.url.startswith('https://'):
        score += 1_000_000
    return score


def probe_with_retry(channel: Channel, settings: dict) -> int:
    for _attempt in range(max(1, int(settings.get('probe_retries', 2)))):
        score = probe(channel, settings)
        if score >= 0:
            return score
    return -1


def inspect_manifest(url: str, channel: Channel, settings: dict) -> str | None:
    headers = {
        'User-Agent': channel.user_agent or settings.get('user_agent', DEFAULT_UA),
    }
    if channel.referrer:
        headers['Referer'] = channel.referrer
    try:
        with urlopen(Request(url, headers=headers), timeout=12) as response:
            return response.read(200_000).decode('utf-8', errors='replace')
    except Exception:
        return None


def black_frame_ok(channel: Channel, settings: dict) -> bool:
    """Отсекает потоки, у которых первые секунды — чёрный экран."""
    command = [
        'ffmpeg', '-hide_banner', '-v', 'info',
        '-user_agent', channel.user_agent or settings.get('user_agent', DEFAULT_UA),
    ]
    if channel.referrer:
        command += ['-headers', f'Referer: {channel.referrer}\r\n']
    command += [
        '-ss', '2',
        '-i', channel.url,
        '-t', str(int(settings.get('black_check_seconds', 8))),
        '-vf', 'blackdetect=d=4:pix_th=0.10',
        '-f', 'null', '-',
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=int(settings.get('decode_timeout_seconds', 15)) + 8,
        )
    except (subprocess.TimeoutExpired, OSError):
        return True  # не удалось проверить — не наказываем
    if result.returncode != 0:
        return False
    return 'black_start:' not in result.stderr


def hls_freshness_ok(channel: Channel, settings: dict) -> bool:
    """Отсекает VOD/заглушки и сильно просроченные HLS-плейлисты."""
    url = channel.url
    if '.m3u8' not in url.lower():
        return True
    if not settings.get('hls_check', True):
        return True

    body = inspect_manifest(url, channel, settings)
    if body is None:
        return True  # проверить не удалось — не наказываем stream
    if '#EXT-X-ENDLIST' in body or 'PLAYLIST-TYPE:VOD' in body.upper():
        return False
    lowered = body.lower()
    if any(word in lowered for word in ('geoblock', 'geo-block', 'not available', 'access denied')):
        return False

    dates = re.findall(r'#EXT-X-PROGRAM-DATE-TIME:([^\r\n]+)', body)
    if not dates:
        variants = [
            line.strip() for line in body.splitlines()
            if line.strip() and not line.startswith('#')
        ]
        if variants:
            variant_url = urljoin(url, variants[0])
            variant = inspect_manifest(variant_url, channel, settings)
            if variant and '#EXT-X-ENDLIST' in variant:
                return False
            dates = re.findall(r'#EXT-X-PROGRAM-DATE-TIME:([^\r\n]+)', variant or '')
    if dates:
        try:
            stamp = datetime.fromisoformat(dates[-1].strip().replace('Z', '+00:00'))
            age_minutes = (datetime.now(timezone.utc) - stamp).total_seconds() / 60
            if age_minutes > int(settings.get('max_segment_age_minutes', 120)):
                return False
        except ValueError:
            pass
    return True


def decode_frame_ok(channel: Channel, settings: dict) -> bool:
    """Пробует реально декодировать кадр — ловит «живые» ffprobe-заглушки."""
    command = [
        'ffmpeg', '-v', 'error',
        '-user_agent', channel.user_agent or settings.get('user_agent', DEFAULT_UA),
    ]
    if channel.referrer:
        command += ['-headers', f'Referer: {channel.referrer}\r\n']
    command += ['-i', channel.url, '-frames:v', '1', '-f', 'null', '-']
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=int(settings.get('decode_timeout_seconds', 15)),
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def check_streams(channels: list[Channel], settings: dict, no_probe: bool) -> None:
    if no_probe:
        for channel in channels:
            channel.score = 1
        return
    for channel in channels:
        if channel.forced:
            channel.score = 1

    workers = int(settings.get('parallel', 16))
    log(f'Проверяю {len(channels)} потоков ({workers} параллельно) через ffprobe…')
    with ThreadPoolExecutor(max_workers=workers) as pool:
        to_check = [channel for channel in channels if not channel.forced]
        futures = {pool.submit(probe_with_retry, channel, settings): channel for channel in to_check}
        for future in as_completed(futures):
            futures[future].score = future.result()
    alive = [channel for channel in channels if channel.score >= 0]
    log(f'Живых потоков после ffprobe: {len(alive)} из {len(channels)}')

    if alive and settings.get('decode_check', True):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(decode_frame_ok, channel, settings): channel for channel in alive
            }
            for future in as_completed(futures):
                if not future.result():
                    futures[future].score = -1
        alive = [channel for channel in channels if channel.score >= 0]
        log(f'Живых потоков после декодирования кадра: {len(alive)}')

    if alive and settings.get('black_check', True):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(black_frame_ok, channel, settings): channel for channel in alive
            }
            for future in as_completed(futures):
                if not future.result():
                    futures[future].score = -1
        alive = [channel for channel in channels if channel.score >= 0]
        log(f'Живых потоков после проверки на чёрный экран: {len(alive)}')

    if alive and settings.get('hls_check', True):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(hls_freshness_ok, channel, settings): channel for channel in alive
            }
            for future in as_completed(futures):
                if not future.result():
                    futures[future].score = -1
        log(f'Живых потоков после проверки HLS: '
            f'{sum(1 for channel in channels if channel.score >= 0)}')


def write_playlist(streams: list[Channel], output_dir: Path, settings: dict, base_url: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    epg_sources = list(settings.get('extra_epg_urls', []))
    epg_sources.append(f'{base_url.rstrip("/")}/epg.xml.gz')
    epg_attr = ','.join(epg_sources)
    refresh = int(settings.get('refresh_hours', 6))
    unique_channels = len({stream.ch_number for stream in streams})
    lines = [
        f'#EXTM3U url-tvg="{epg_attr}" refresh="{refresh}"',
        f'# Generated {datetime.now(timezone.utc).isoformat()} — '
        f'{unique_channels} channels / {len(streams)} streams',
        f'# Live streams: {sum(1 for c in streams if c.score >= 0)}, '
        f'backup streams: {sum(1 for c in streams if c.score < 0)}',
    ]

    def safe(value: str) -> str:
        return value.replace('"', "'")

    for channel in streams:
        name = channel.official_name or clean_official(channel.name)
        attrs = [f'ch-number="{channel.ch_number}"']
        if channel.epg_id:
            attrs.append(f'tvg-id="{safe(channel.epg_id)}"')
        if channel.tvg_shift:
            attrs.append(f'tvg-shift="{channel.tvg_shift}"')
        attrs.append(f'tvg-name="{safe(name)}"')
        if channel.logo and not (
            channel.epg_id and settings.get('use_epg_logo_for_matched', False)
        ):
            attrs.append(f'tvg-logo="{safe(channel.logo)}"')
        attrs.append(f'group-title="{safe(channel.category or "Прочее")}"')
        lines.append(f'#EXTINF:-1 {" ".join(attrs)},{name}')
        if channel.user_agent:
            lines.append(f'#EXTVLCOPT:http-user-agent={channel.user_agent}')
        if channel.referrer:
            lines.append(f'#EXTVLCOPT:http-referrer={channel.referrer}')
        lines.append(channel.url)
    (output_dir / 'playlist.m3u').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    log(f'Плейлист записан: {unique_channels} каналов, {len(streams)} потоков')


def write_status(prepared: list[dict], output_dir: Path, report: dict) -> None:
    """Публикует простой отчёт о рабочих и резервных каналах."""
    dead = [
        item['name'] for item in prepared
        if not any(stream.score >= 0 for stream in item['streams'])
    ]
    lines = [
        f"# Статус плейлиста — {report['generated_at']}",
        '',
        f"Каналов всего: {report['channels']}",
        f"Потоков всего: {report['streams']}",
        f"Рабочих потоков: {report['live_streams']}",
        f"Резервных потоков в плейлисте: {report['backup_streams']}",
        f"Отслеживается потоков всего: {report['tracked_streams']}",
        f"Из них сейчас нерабочих: {report['tracked_dead']}",
        f"Каналов без рабочего потока: {len(dead)}",
        '',
    ]
    if dead:
        lines.append('Каналы только с резервными (сейчас не отвечают):')
        lines.extend(f'- {name}' for name in dead)
    else:
        lines.append('Все каналы имеют хотя бы один рабочий поток.')
    (output_dir / 'status.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    log(f'Статус записан: {len(dead)} каналов без рабочего потока')


def write_epg(
    epg_path: Path,
    output_dir: Path,
    selected_ids: set[str],
    xml_by_id: dict[str, str],
    tv_attrs: dict[str, str],
) -> None:
    target = output_dir / 'epg.xml.gz'
    attributes = ' '.join(f'{key}={quoteattr(value)}' for key, value in tv_attrs.items())
    with gzip.open(target, 'wt', encoding='utf-8', newline='\n') as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        out.write('<!DOCTYPE tv SYSTEM "https://iptvx.one/xmltv.dtd">\n')
        out.write(f'<tv {attributes}>\n')
        for channel_id in sorted(selected_ids):
            if channel_id in xml_by_id:
                out.write(xml_by_id[channel_id] + '\n')
        with gzip.open(epg_path, 'rb') as source:
            for _event, element in ET.iterparse(source, events=('end',)):
                if element.tag == 'programme' and element.get('channel') in selected_ids:
                    out.write(ET.tostring(element, encoding='unicode') + '\n')
                if element.tag in ('channel', 'programme'):
                    element.clear()
        out.write('</tv>\n')
    log(f'EPG записан: {len(selected_ids)} каналов, {target.stat().st_size // 1024} КБ')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config' / 'settings.json')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'output')
    parser.add_argument('--epg-file', type=Path, help='Локальный epg.xml.gz для теста')
    parser.add_argument('--no-probe', action='store_true', help='Не проверять потоки (тест логики)')
    args = parser.parse_args()

    settings = read_settings(args.config)
    source_file = ROOT / settings.get('sources_file', 'config/sources.txt')
    base_url = os.environ.get('BASE_URL') or settings.get('base_url') or (
        f'https://{settings.get("github_user", "USER")}.github.io/'
        f'{settings.get("github_repo", "REPO")}/'
    )

    epg_context = (
        nullcontext(args.epg_file.resolve()) if args.epg_file else _download_epg(settings)
    )
    with epg_context as epg_path:
        by_id, raw_by_id, xml_by_id, tv_attrs = load_epg_channels(epg_path)
        name_to_ids, _name_keys = build_name_index(by_id)
        token_index = build_token_index(name_to_ids)
        priority_names = {normalize(name) for name in settings.get('priority_channels', [])}
        excluded_names = {normalize(name) for name in settings.get('exclude_channels', [])}
        canonical_names = load_canonical_names(settings)
        database_names, database_categories = load_database_names(settings)
        settings['_database_categories'] = database_categories
        name_overrides = settings.get('name_overrides', {})

        channels: list[Channel] = []
        for line in source_file.read_text(encoding='utf-8').splitlines():
            url = line.strip()
            if not url or url.startswith('#'):
                continue
            try:
                if url.startswith(('http://', 'https://')):
                    text = fetch_text(url)
                else:
                    text = (ROOT / url).read_text(encoding='utf-8-sig')
                found = parse_m3u(text, url)
                channels.extend(found)
                log(f'Источник {url}: {len(found)} каналов')
            except Exception as error:  # источник может временно лежать
                log(f'Источник {url} недоступен: {error}')

        state_path = ROOT / settings.get('state_file', 'state/known_streams.json')
        channels.extend(
            load_known_streams(
                state_path,
                int(settings.get('state_max_age_days', 14)),
            )
        )
        forced_urls = set(settings.get('force_include_urls', []))
        id_overrides = settings.get('epg_id_overrides', {})
        for channel in channels:
            if channel.url in forced_urls:
                channel.forced = True
                channel.score = 1
            if channel.epg_id in id_overrides:
                channel.epg_id = id_overrides[channel.epg_id]

        kept: list[Channel] = []
        reserve: list[Channel] = []
        seen_urls: set[str] = set()
        for channel in channels:
            if is_blocked(channel, settings):
                continue
            if channel.norm_name in excluded_names or normalize(channel.name) in excluded_names:
                continue
            url_key = channel.url.strip()
            if not url_key or url_key in seen_urls:
                continue
            if not settings.get('include_shifted_channels', False) and channel.tvg_shift:
                continue
            seen_urls.add(url_key)
            if in_focus(channel, settings) or is_priority(channel, priority_names):
                channel.epg_id, channel.epg_score = match_epg(
                    channel, by_id, name_to_ids, token_index
                )
                if channel.epg_id in id_overrides:
                    channel.epg_id = id_overrides[channel.epg_id]
                kept.append(channel)
            else:
                reserve.append(channel)

        minimum = int(settings.get('min_channels', 20))
        if len(kept) < minimum:
            log('Мало каналов в фокусе — пробую спасти каналы с совпавшим EPG')
            for channel in reserve:
                epg_id, epg_score = match_epg(channel, by_id, name_to_ids, token_index)
                if epg_id:
                    channel.epg_id, channel.epg_score = epg_id, epg_score
                    kept.append(channel)

        log(f'После фильтрации: {len(kept)} потоков')
        check_streams(kept, settings, args.no_probe)
        save_known_streams(state_path, kept)

        groups: dict[str, list[Channel]] = {}
        for channel in kept:
            group_key = channel.norm_name
            if channel.tvg_shift:
                group_key = f'{group_key}|{channel.tvg_shift:+d}'
            groups.setdefault(group_key, []).append(channel)
        max_streams = int(settings.get('max_streams_per_channel', 3))
        prepared: list[dict] = []
        publish_dead = bool(settings.get('keep_dead_streams', False))
        min_sources = int(settings.get('min_sources', 1))
        for norm_name, options in groups.items():
            independent_sources = {
                channel.source for channel in options if channel.source != 'state'
            }
            if (
                min_sources > 1
                and len(independent_sources) < min_sources
                and not any(channel.forced for channel in options)
                and not is_priority(options[0], priority_names)
            ):
                continue
            candidates = [channel for channel in options if channel.score >= 0]
            if not candidates:
                # Канал без живого потока в плейлист не попадает,
                # но все его URL остаются в state и проверяются в следующих запусках.
                if not publish_dead:
                    continue
                candidates = list(options)
            elif publish_dead:
                candidates.extend(channel for channel in options if channel.score < 0)
            candidates.sort(
                key=lambda item: (
                    item.score >= 0,
                    item.score,
                    is_priority(item, priority_names),
                    item.epg_score,
                    bool(item.epg_id),
                ),
                reverse=True,
            )
            working = candidates[:max_streams]
            group_epg_id = next((c.epg_id for c in working if c.epg_id), '')
            group_shift = next((c.tvg_shift for c in working if c.tvg_shift), 0)
            for stream in working:
                if group_epg_id:
                    stream.epg_id = group_epg_id
                if group_shift:
                    stream.tvg_shift = group_shift
            best = working[0]
            base_id = best.tvg_id.split('@')[0].lower()
            official = (
                name_overrides.get(norm_name)
                or canonical_names.get(best.epg_id)
                or database_names.get(base_id)
                or (
                    pick_display_name(raw_by_id.get(best.epg_id, []), best.name)
                    if best.epg_id
                    else clean_official(best.name)
                )
            )
            shift_in_name = SHIFT_RE.search(official)
            if shift_in_name and not best.tvg_shift:
                detected = int(shift_in_name.group(1))
                if -12 <= detected <= 12:
                    best.tvg_shift = detected
            official = with_source_details(official, best.name, best.tvg_shift)
            category = assign_category(best, settings)
            for stream in working:
                stream.official_name = official
                stream.category = category
                stream.tvg_shift = best.tvg_shift
            prepared.append({'name': official, 'category': category, 'streams': working})

        epg_by_name = {
            normalize(item['name']): item['streams'][0].epg_id
            for item in prepared
            if item['streams'][0].epg_id
        }
        for item in prepared:
            if item['streams'][0].epg_id:
                continue
            inherited = epg_by_name.get(normalize(item['name']))
            if inherited:
                for stream in item['streams']:
                    stream.epg_id = inherited

        merged: dict[str, dict] = {}
        for item in prepared:
            key = normalize(item['name'])
            if key not in merged:
                merged[key] = item
                continue
            known_urls = {stream.url for stream in merged[key]['streams']}
            for stream in item['streams']:
                if stream.url not in known_urls:
                    known_urls.add(stream.url)
                    merged[key]['streams'].append(stream)
        prepared = list(merged.values())
        for item in prepared:
            name = name_overrides.get(normalize(item['name']), item['name'])
            item['name'] = name
            for stream in item['streams']:
                stream.official_name = name
            category = assign_category(item['streams'][0], settings)
            item['category'] = category
            for stream in item['streams']:
                stream.category = category

        category_order = settings.get('category_order', [])

        def order_key(item: dict) -> tuple[int, str]:
            category = item['category']
            try:
                position = category_order.index(category)
            except ValueError:
                position = len(category_order)
            return position, item['name'].lower()

        prepared.sort(key=order_key)
        final_streams: list[Channel] = []
        for number, item in enumerate(prepared, start=1):
            for stream in item['streams']:
                stream.ch_number = number
                final_streams.append(stream)

        if len(prepared) < minimum:
            log(f'ОШИБКА: живых каналов {len(prepared)} < минимума {minimum}; публикация отменена')
            return 2

        write_playlist(final_streams, args.output_dir, settings, base_url)
        write_epg(
            epg_path,
            args.output_dir,
            {c.epg_id for c in final_streams if c.epg_id},
            xml_by_id,
            tv_attrs,
        )

        report = {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'channels': len(prepared),
            'streams': len(final_streams),
            'live_streams': sum(1 for channel in final_streams if channel.score >= 0),
            'backup_streams': sum(1 for channel in final_streams if channel.score < 0),
            'tracked_streams': len(kept),
            'tracked_dead': sum(1 for channel in kept if channel.score < 0),
            'with_epg': sum(1 for item in prepared if item['streams'][0].epg_id),
            'categories': sorted({item['category'] for item in prepared}),
            'sources': len([line for line in source_file.read_text(encoding='utf-8').splitlines()
                            if line.strip() and not line.startswith('#')]),
            'base_url': base_url,
        }
        (args.output_dir / 'report.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
        )
        write_status(prepared, args.output_dir, report)
        log(f'Готово: {report["channels"]} каналов, {report["streams"]} потоков, '
            f'{report["with_epg"]} с EPG')
    return 0


class _download_epg:
    """Контекстный менеджер: скачивает EPG во временный файл и удаляет его."""

    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self.path: Path | None = None

    def __enter__(self) -> Path:
        import tempfile

        directory = Path(tempfile.mkdtemp(prefix='iptv-epg-'))
        self.path = directory / 'epg.xml.gz'
        epg_url = os.environ.get('EPG_URL') or self.settings['epg_url']
        log(f'Скачиваю EPG: {epg_url}')
        fetch_binary(epg_url, self.path)
        log(f'EPG скачан: {self.path.stat().st_size // (1024 * 1024)} МБ')
        return self.path

    def __exit__(self, *_args: object) -> None:
        if self.path:
            self.path.unlink(missing_ok=True)
            self.path.parent.rmdir()


if __name__ == '__main__':
    sys.exit(main())
