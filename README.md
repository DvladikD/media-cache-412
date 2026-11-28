# Автообновляемый IPTV-плейлист (OTT Navigator, Россия и СНГ)

Рабочая установка: **`DvladikD/media-cache-412`**. Репозиторий публичный, переименован на нейтральное имя, чтобы не привлекать внимание.

**Плейлист для OTT Navigator:**

```
https://DvladikD.github.io/media-cache-412/playlist.m3u
```

Добавлять именно **ссылку**, не файл: плейлист из файла плеер не обновляет.

## Как это работает

Каждые 6 часов GitHub Actions:

1. скачивает источники из `config/sources.txt` (публичные списки + локальные `config/dnr.m3u`, `config/favorites.m3u`);
2. отбирает каналы России/СНГ и ДНР, дедуплицирует потоки, сопоставляет `tvg-id` с XMLTV;
3. проверяет каждый поток: `ffprobe` с повторами, реальное декодирование кадра, чёрный экран, свежесть HLS-сегментов, geo/VOD-заглушки;
4. публикует только живые потоки (до `max_streams_per_channel` на канал); нерабочие остаются в `state/known_streams.json` и возвращаются автоматически, если оживут;
5. публикует на GitHub Pages: `playlist.m3u`, `epg.xml.gz`, `report.json`, `status.txt`.

В шапке плейлиста — `url-tvg` (сначала `epg.it999.ru`, затем свой `epg.xml.gz`) и `refresh="6"`.

## Что уже настроено в OTT Navigator

- `Settings → Provider data → Channel processing → Duplicate channels → Fold duplicate channels to a single element` (**Premium**) — объединение дубликатов и авто-переключение при ошибке; режим «by name within one category».
- Сортировка: `order from provider playlist`, чтобы сохранить порядок категорий и `ch-number`.

## Структура

| Путь | Назначение |
|---|---|
| `scripts/build.py` | сборщик плейлиста и EPG |
| `config/sources.txt` | источники (одна ссылка на строку; `config/*.m3u` — локальные списки) |
| `config/settings.json` | фильтры, категории, приоритеты, лимиты проверки |
| `config/dnr.m3u` | ДНР/Донбасс: Первый Республиканский, Оплот, Новороссия |
| `config/favorites.m3u` | Русский детектив, Чип и Дейл, Сити Эдем КиноДетектив |
| `state/known_streams.json` | все известные потоки с статусом (живой/нерабочий) |
| `state/last_run.json` | статистика последнего запуска |
| `CHANGELOG.md` | история обновлений |

## Что можно менять

- `config/sources.txt` — добавлять/удалять источники.
- `config/settings.json`:
  - `priority_channels` / `force_include_urls` — каналы и URL, которые нельзя терять;
  - `exclude_channels` / `exclude_words` — ручное исключение;
  - `dnr_keywords`, `cng_name_keywords`, `russian_name_keywords`, `database_category_map` — категории;
  - `min_sources` — минимум независимых источников (сейчас 1);
  - `parallel`, `probe_retries`, `decode_check`, `black_check`, `hls_check` — жёсткость и скорость проверки;
  - `min_channels` — порог, ниже которого публикация отменяется.

## Проверка состояния

- Плейлист: `https://DvladikD.github.io/media-cache-412/playlist.m3u`
- Статус: `https://DvladikD.github.io/media-cache-412/status.txt`
- Отчёт: `https://DvladikD.github.io/media-cache-412/report.json`
- Запуски: `https://github.com/DvladikD/media-cache-412/actions`
- История: [CHANGELOG.md](CHANGELOG.md)

## Локальная проверка (не обязательно)

Нужны Python 3.11+ и `ffmpeg`:

```powershell
python scripts/build.py --no-probe --epg-file .epg-cache\epg.xml.gz
```

Полный прогон с проверкой потоков: `python scripts/build.py`.

## Важное

- Публичные потоки нестабильны и могут быть геоблокированы; стопроцентной гарантии нет.
- ByeByeDPI на GitHub runner не поможет ТВ — обход DPI нужен в домашней сети (роутер или само ТВ).
- Репозиторий содержит только публичные ссылки, без токенов и логинов.
- Просмотр — только каналов, доступных для свободного просмотра в вашем регионе.
