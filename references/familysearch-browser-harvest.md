# Последовательный сбор индекса в браузере

Читать при сборе всей выбранной книги. Использовать текущую сессию и браузерные инструменты среды, как в основном навыке. Пользователь не запускает загрузчик и не переносит сессию. Поиск места, DGS и выбор части — обычный интерфейс Search / Images; сохранять наблюдённые название, местность, годы, группу, DGS и число кадров. Один DGS может содержать несколько частей с разным числом кадров.

## Порции и продолжение

1. Создать локальный план командой `familysearch.py harvest` из `familysearch-indexes.md`.
2. Открыть Names выбранной книги. Получить свежий DOM и проверить поле номера кадра, границы панели, строки имён и индикаторы загрузки.
3. Прочитать `next_frames` последовательно, короткими порциями. Сохранять **все** строки каждого кадра, а не только совпадения. Если список виртуализован, дочитать его прокруткой до конца до сохранения кадра.
4. Вывести JSON порции из браузерного инструмента; сохранить его обычным файловым инструментом как `familysearch-browser-book` с тем же `book`. Не просить пользователя копировать данные. Не сохранять большой объём только в памяти REPL: после каждой порции принять файл через `harvest --batch`.
5. Продолжить со следующим `next_frames`. После прерывания использовать сохранённый план. `failed` можно заменить успешным наблюдением; завершённые кадры не затираются. При завершении выполнить `browser-import`, перенести кандидатов в существующее дело и проверить оригиналы.

## Пример для браузерного инструмента Codex

Это код действий агента в `cua_repl`, не отдельный Node.js-загрузчик. Прочитать документацию текущего инструмента; пример использует только локаторы для переключения кадров и чтение отображённого DOM. Селекторы ниже проверены на английской панели Names; перед использованием сверить их с текущей страницей. Если панель или язык изменились, адаптировать по наблюдаемому интерфейсу. Не заменять недоступный метод скрытым сетевым вызовом.

```javascript
var collectFsFrame = async function (tab, number, book) {
  const field = tab.playwright.getByRole('spinbutton', {
    name: 'Enter Image number', exact: true
  });
  await field.fill(String(number));
  await field.press('Enter');
  await tab.getAXState({emit: false});
  const panel = tab.playwright.locator('div.mainWrapperCss_mer28f7').filter({
    has: tab.playwright.getByRole('heading', {name: 'Names', exact: true})
  });
  const skeleton = panel.locator('[class*="skeletonCss_"]').first();
  await skeleton.waitFor({state: 'hidden', timeoutMs: 20000});
  await panel.locator('[role="button"][data-testid]').first().or(
    panel.getByText('This image has not been indexed.', {exact: true})
  ).first().waitFor({state: 'visible', timeoutMs: 20000});
  await skeleton.waitFor({state: 'hidden', timeoutMs: 20000});
  return tab.playwright.evaluate(({n, book}) => {
    const input = document.querySelector('input[aria-label="Enter Image number"]');
    const heading = Array.from(document.querySelectorAll('h2'))
      .find(el => el.innerText === 'Names');
    const panel = heading?.closest('header')?.parentElement;
    if (!panel || Number(input?.value) !== n ||
        panel.querySelector('[class*="skeletonCss_"]')) {
      throw Error('Frame not ready');
    }
    const url = new URL(document.location.href);
    if (url.searchParams.get('groupId') !== book.group_id ||
        Number(input.max) !== book.expected_frames) {
      throw Error('Selected book changed');
    }
    url.searchParams.delete('personArk');
    url.searchParams.delete('action');
    const groups = Array.from(panel.querySelectorAll('ul')).filter(ul =>
      ul.getAttribute('role') !== 'menu' &&
      ul.querySelector('[role="button"][data-testid]'));
    const rows = groups.map((ul, i) => {
      const people = Array.from(ul.querySelectorAll('[role="button"][data-testid]'))
        .map(el => ({name: el.getAttribute('data-testid'), label: el.innerText}));
      return {entry: i + 1, name: people[0].name, text: ul.innerText, people};
    });
    const unindexed = panel.innerText.includes('This image has not been indexed.');
    if (!rows.length && !unindexed) throw Error('Unknown index state');
    return {number: n, url: url.href, captured_at: new Date().toISOString(),
      status: rows.length ? 'indexed' : 'not-indexed', loading: false, rows};
  }, {n: number, book});
};
```

Вызывать для номеров из плана на уже выбранной вкладке, например `await collectFsFrame(tab, number, book)`. Число ожидаемых кадров и `group_id` брать из наблюдённой книги, не из примера. Не запускать длинный цикл без сохранения: максимум пять кадров в порции, при объёмных строках меньше. В других средах выполнять тот же цикл доступным браузерным инструментом; отдельная среда исполнения JavaScript пользователю не требуется.

Таймаут готовности — повод осмотреть текущую страницу. Не записывать его как отсутствие индекса. При обычном временном сбое допустим повтор после проверки страницы; при CAPTCHA, отказе доступа или ограничении запросов сохранить выполненное, отметить блокировку и следовать `browser-and-captcha.md`. Не извлекать cookies, не читать внутреннее состояние приложения и не вызывать внутренний API вместо браузерных действий.

Снимок Names отражает строки индекса. Подробные даты, места, варианты имён и роли проверять в открываемых карточках, сохраняя фактическую ссылку и текст. Полное покрытие кадров не означает полноту полей карточек или расшифровки книги. Индекс и его изображение остаются одним исходным документом при оценке независимости.
