# Website Auditor

`Website Auditor` — консольный инструмент для базового аудита сайта. Скрипт начинает обход с указанного URL, проверяет несколько страниц в пределах того же домена и сохраняет результаты в CSV-отчёты.

## Возможности

- принимает стартовый URL и лимит страниц через аргументы командной строки
- поддерживает настройку таймаута запроса и запуск без SSL-проверки для проблемных сертификатов
- отправляет HTTP-запросы к страницам сайта
- определяет HTTP-статус ответа и время ответа сервера
- отслеживает итоговый URL после редиректов и количество редиректов
- извлекает содержимое тега `<title>`
- оценивает базовые SEO-сигналы: `meta description`, `canonical`, наличие `H1`, дубликаты `title` и `meta description`
- определяет директивы `noindex` и `nofollow` из `meta robots`
- проверяет длину `title` и `meta description`
- проверяет `lang`, `hreflang`, Open Graph и Twitter meta-теги
- помечает страницы, где `canonical` не совпадает с итоговым URL
- определяет внешний `canonical` и mixed content на HTTPS-страницах
- определяет длинные цепочки редиректов и подозрение на soft 404
- считает страницы без входящих внутренних ссылок внутри crawl-графа
- проверяет изображения и находит `img` без `alt` или с пустым `alt`
- находит внутренние ссылки на странице
- продолжает обход страниц того же домена
- фиксирует битые ссылки, если запрос не удался или сервер вернул код `400+`
- повторяет нестабильные запросы при временных ошибках сервера
- проверяет наличие и доступность `robots.txt` и `sitemap.xml`
- ищет orphan-страницы: URL из sitemap, которые не попали в обход
- генерирует HTML-отчёт для просмотра результатов в браузере
- сохраняет результаты в `pages_report.csv`, `broken_links_report.csv`, `site_report.csv`, `summary_report.csv`, `orphan_pages_report.csv`, `image_issues_report.csv`, `issues_report.csv`, `audit_report.html` и `audit_report.json`

## Используемые технологии

- Python 3
- `requests`
- `beautifulsoup4`
- стандартные библиотеки `csv` и `argparse`

## Структура проекта

```text
website-auditor/
├── auditor.py
├── requirements.txt
└── README.md
```

## Установка зависимостей

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Запуск программы

```bash
python auditor.py https://example.com --max-pages 10
```

Дополнительно:

```bash
python auditor.py https://example.com --max-pages 20 --timeout 15 --allow-insecure
```

## Формат выходных файлов

### `pages_report.csv`

Содержит:

- URL страницы
- итоговый URL после редиректов
- HTTP-статус
- время ответа в миллисекундах
- количество редиректов
- заголовок страницы
- длину `title`
- флаги короткого или длинного `title`
- длину `meta description`
- текст `meta description`
- флаги короткого или длинного `meta description`
- директивы `meta robots`
- флаги `noindex` и `nofollow`
- `lang` страницы
- количество `hreflang`
- флаги наличия `og:title`, `og:description`, `og:image`, `twitter:card`
- `canonical URL`
- флаг несоответствия `canonical` итоговому URL
- флаг внешнего `canonical`
- флаг длинной цепочки редиректов
- флаг подозрения на soft 404
- флаги mixed content и количество HTTP-ресурсов на HTTPS-странице
- количество входящих внутренних ссылок
- флаг отсутствия входящих внутренних ссылок
- количество тегов `H1`
- количество изображений на странице
- количество изображений без `alt`
- количество изображений с пустым `alt`
- количество найденных внутренних ссылок
- флаги проблем: отсутствует `title`, `meta description`, `H1` или найдены дубликаты `title`/`meta description`

### `broken_links_report.csv`

Содержит:

- страницу, на которой найдена ссылка
- битую ссылку
- статус ответа или признак ошибки запроса

### `site_report.csv`

Содержит:

- корневой адрес сайта
- URL и статус `robots.txt`
- признак наличия `robots.txt`
- URL и статус `sitemap.xml` или sitemap из `robots.txt`
- признак наличия sitemap
- количество URL, найденных в sitemap

### `summary_report.csv`

Содержит агрегированные метрики:

- сколько страниц было обойдено
- сколько найдено битых ссылок
- сколько страниц без `title`, `meta description`, `H1`
- сколько страниц без `lang`
- сколько страниц с коротким или длинным `title` и `meta description`
- сколько страниц с дублирующимся `title` и `meta description`
- сколько страниц без Open Graph и Twitter meta-тегов
- сколько страниц отмечены `noindex` и `nofollow`
- сколько страниц имеют `canonical mismatch` или внешний `canonical`
- сколько страниц имеют длинную цепочку редиректов или soft 404
- сколько страниц имеют mixed content и сколько HTTP-ресурсов найдено
- сколько страниц не имеют входящих внутренних ссылок внутри crawl-графа
- доступны ли `robots.txt` и sitemap
- сколько URL найдено в sitemap и сколько orphan-страниц обнаружено

### `orphan_pages_report.csv`

Содержит:

- URL страницы из sitemap
- признак наличия URL в sitemap
- признак того, что страница не была обойдена краулером

### `image_issues_report.csv`

Содержит:

- страницу, на которой найдено изображение
- URL изображения
- тип проблемы (`missing_alt` или `empty_alt`)
- текущее значение `alt`

### `issues_report.csv`

Содержит единый список проблем:

- уровень проблемы (`site`, `page`, `link`, `image`, `sitemap`)
- источник проблемы
- тип проблемы
- severity
- детали

### `audit_report.json`

Содержит полную структурированную выгрузку для CI и интеграций:

- summary
- site report
- pages report
- broken links
- orphan pages
- image issues
- unified issues

### `audit_report.html`

Содержит визуальный дашборд:

- общую сводку метрик
- таблицу страниц и SEO-флагов
- единый список проблем
- битые ссылки
- проблемы изображений
- orphan-страницы из sitemap
