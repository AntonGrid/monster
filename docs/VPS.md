# Неделя данных на VPS — инструкция для Антона

Цель одна: **семь суток непрерывной записи HTX, OKX и Bybit**, из которой потом
можно без оговорок строить предрегистрацию Funding Carry. Всё, что делает эта
инструкция, — запускает уже написанный и протестированный сборщик и проверяет,
что он пишет именно то, что должен.

Ключи не нужны: все эндпоинты публичные, а пакет не умеет отправлять ордера.

## 0. Что считается успехом

Через 7 суток у нас должно быть:

| Проверка | Команда | Порог |
|---|---|---|
| покрытие | `python -m monster.data gaps --store var/data --interval 60` | ≥ 98 % слотов у каждого `(биржа, символ)` |
| воспроизводимость | `python -m monster.data audit --store var/data --day <день>` | 100 % ключей: raw → normalized |
| сбор идёт | `tail var/log/capture.log` | строка в минуту, `18 reading(s), 18 raw body(ies)` |
| журнал цел | `python -m monster.cycle --verify` | `ok (N events)` и `links: k ok` |
| ворота Фазы 1 | `python -m monster.gate --store var/data --day 2026-11-20 … 2026-11-24` | exit 0 по правилам `docs/prereg/2026-11-19-truth-materiality-floor.md` |

Порог материальности (3 bps) и окно hold-out заморожены **19.11, до чтения
hold-out**; 17–18.11 остаются калибровкой, 19.11 — днём утечки, в выборку не
входит.  Поэтому «семь суток» здесь означают пять суток, на которых ворота
считаются, плюс два дня, на которых они писались.


Если покрытие ниже — это не «почти получилось»: это другой набор данных, и он
будет назван, а не подтянут интерполяцией.

## 1. Что должно быть на VPS

```bash
python3 --version                 # нужно >= 3.10
timedatectl | grep -E 'synchronized|Time zone'   # часы обязаны быть синхронны
df -h "$HOME"                     # нужно >= 2 GB свободно
curl -s -o /dev/null -w '%{http_code}\n' 'https://api.hbdm.com/linear-swap-ex/market/detail/merged?contract_code=BTC-USDT'
```

**Часы — не формальность.** Время получения котировки пишем мы, а не биржа:
если часы VPS уплывут, `derive` начнёт отвергать свежие котировки как старые
(или принимать старые как свежие), и вся неделя будет измерять дрейф часов, а
не рынок. `System clock synchronized: yes` — обязательное условие.

Если `synchronized: no`:

```bash
sudo timedatectl set-ntp true && sudo systemctl restart systemd-timesyncd
```

## 2. Установка

Сборщику нужен только Python и стандартная библиотека; `numpy`/`scipy` нужны
только команде `monster.cycle`, но поставим всё, чтобы вечером не выяснять,
чего не хватает.

```bash
mkdir -p "$HOME/entrain-core-workspace" && cd "$HOME/entrain-core-workspace"
# вариант А: git — оба репозитория, чтобы работал monster.cycle (когерентность
# берётся из ENRG-AI, одна копия арифметики на всю экосистему).
# ENRG-AI должен лежать рядом с monster/ — мост ищет именно этот сосед.
git clone https://github.com/AntonGrid/monster.git
git clone https://github.com/AntonGrid/ENRG-AI.git
# (по SSH — те же адреса с git@github.com:, если ключ на VPS есть;
#  для приватного ENRG-AI без ключа используй токен в URL)
# вариант Б: с ноутбука (без git-истории, только код)
#   rsync -av --exclude '.venv' --exclude '__pycache__' \
#     ~/entrain-core-workspace/monster/ anton@<vps>:~/entrain-core-workspace/monster/
#   (ENRG-AI рядом: ~/entrain-core-workspace/ENRG-AI, либо MONSTER_ENRG_AI=/path/to/ENRG-AI)

cd monster
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .          # monster + numpy + scipy
.venv/bin/python -m monster.data --help | head -3
```

> Команды `probe`, `capture`, `audit`, `gaps`, `doctor`, `derive` не импортируют
> `ENRG-AI`: если соседнего репозитория на VPS нет, они работают всё равно.
> `monster.cycle` ищет его в `../ENRG-AI`, `../../ENRG-AI` или по
> `MONSTER_ENRG_AI`; `monster.cycle --verify` (проверка журнала) работает и без
> него, а полный цикл без ENRG-AI честно откажется читать когерентность.

## 3. Первый контакт: probe

Один запрос к каждому из 18 эндпоинтов, без записи на диск, без ретраев.
Здесь и только здесь видно, что биржа отвечает **той форме, которую мы
разбираем**, а не просто `200`.

```bash
.venv/bin/python -m monster.data probe --live
```

Ждём ровно это (вывод с машины разработчика, 17.11.2026; на VPS числа и
задержки будут другими, форма та же):

```
  [ok]   bybit funding-btc  +0.188 bps/8h               774 ms
  [ok]   bybit spot-btc     63766.5 / 63766.6           542 ms
  [ok]   htx   funding-btc  +1.000 bps/8h               798 ms
  [ok]   htx   spot-btc     63799.4 / 63799.4          1283 ms
  [ok]   okx   funding-btc  +0.257 bps/8h               750 ms
  [ok]   okx   perp-btc     63745.6 / 63745.7           583 ms
  ...
probe: 18/18 endpoint(s) answered and parsed
```

Код выхода `0` — все 18. Если нет, смотри §8; запускать неделю при `17/18`
нельзя, но и паниковать не нужно: `probe` ничего не испортил.

## 4. Запуск на неделю

Проход = 16 HTTP-запросов (18 чтений: у Bybit тикер один на спот, перп и
фандинг) и ~1–2 с. Следующий проход стартует по абсолютной сетке, поэтому
долгий ответ не сдвигает расписание: пропущенные слоты становятся `Gap`, а не
«догоняющим залпом».

```bash
cd "$HOME/entrain-core-workspace/monster"
mkdir -p var/log

tmux new -s monster
# внутри tmux:
env PYTHONUNBUFFERED=1 .venv/bin/python -m monster.data capture \
  --live --store var/data --interval 60 --count 10080 \
  --journal var/journal.capture.jsonl --fsync \
  2>&1 | tee -a var/log/capture.log
# отцепиться, оставив работать: Ctrl-b, затем d
```

> `--journal` у сборщика — **свой** файл (`var/journal.capture.jsonl`). Два
> писателя на один журнал перемежают `prev`/`hash` и ломают цепочку, поэтому
> `append` отказывает файлу, который вырос за спиной. Цикл пишет
> `var/journal.jsonl` и связывает цепочку сборщика головой (`journal.linked`):
> `MONSTER_DATA_JOURNAL=var/journal.capture.jsonl`.

`10080` = 7 суток × 24 ч × 60 мин — процесс сам завершится через неделю.
Повторный запуск позже не испортит данные: дубликаты `(venue, symbol, ts)`
отбрасываются, а расхождение под тем же ключом становится `Conflict`, а не
средним.

Проверить, что tmux жив и пишет:

```bash
tmux ls
tmux attach -t monster          # посмотреть; выйти — Ctrl-b d
```

> `PYTHONUNBUFFERED=1` в команде — не украшение: при перенаправлении в файл
> Python буферизует stdout блоками, и «лог пустой» не будет значить «сбор
> стоит».


### Вариант без tmux: systemd (переживает reboot)

> **Сначала — пути.** В шаблонах `deploy/*` нет ни одного абсолютного пути: есть
> ровно два заполнителя, `<USER>` и `<INSTALL_DIR>`. Если ваш checkout лежит не в
> `~/entrain-core-workspace/monster` (например, `/root/monster`), подставьте свой
> путь через `INSTALL_DIR` — иначе юнит запустит несуществующий python или, хуже,
> чужой каталог. Что реально попало в юнит, видно в `systemctl cat monster-cycle`.

```bash
cd "$HOME/entrain-core-workspace/monster"
INSTALL_DIR="$PWD"           # ← путь к checkout; на этом VPS это /root/monster
sed "s#<USER>#$USER#g; s#<INSTALL_DIR>#$INSTALL_DIR#g" deploy/monster-capture.service \
  | sudo tee /etc/systemd/system/monster-capture.service
sudo systemctl daemon-reload
sudo systemctl enable --now monster-capture
systemctl status monster-capture --no-pager
journalctl -u monster-capture -n 20 --no-pager
```

`Restart=always` означает, что падение стоит секунд, а не недели. В tmux
такого нет — если выбираешь tmux, проверяй `tmux ls` хотя бы раз в день.

Плюс ежедневный `audit` таймером (00:10 UTC, по вчерашнему дню):

```bash
INSTALL_DIR="$PWD"
sudo sed "s#<USER>#$USER#g; s#<INSTALL_DIR>#$INSTALL_DIR#g" deploy/monster-audit.service \
  | sudo tee /etc/systemd/system/monster-audit.service
sudo sed "s#<USER>#$USER#g; s#<INSTALL_DIR>#$INSTALL_DIR#g" deploy/monster-audit.timer \
  | sudo tee /etc/systemd/system/monster-audit.timer
sudo systemctl daemon-reload
sudo systemctl enable --now monster-audit.timer
systemctl list-timers monster-audit --no-pager
```

Плюс часовой `probe` — сторож формы ответов, а не данных:

```bash
INSTALL_DIR="$PWD"
sudo sed "s#<USER>#$USER#g; s#<INSTALL_DIR>#$INSTALL_DIR#g" deploy/monster-probe.service \
  | sudo tee /etc/systemd/system/monster-probe.service
sudo sed "s#<USER>#$USER#g; s#<INSTALL_DIR>#$INSTALL_DIR#g" deploy/monster-probe.timer \
  | sudo tee /etc/systemd/system/monster-probe.timer
sudo systemctl daemon-reload
sudo systemctl enable --now monster-probe.timer
tail -5 var/log/probe.log
```

Он ничего не пишет в магазин: спрашивает те же 18 эндпоинтов, разбирает их
теми же парсерами и выходит с кодом 1, если форма ответа уехала. Биржа умеет
отвечать `200` и другим телом — тогда сбор идёт, `audit` сам с собой согласен, а
неделя ничего не стоит. Час — это цена раннего предупреждения; раз в неделю —
цена того, что узнаёшь на восьмой день.

Плюс часовой аудит книги — `--verify` и `--replay` одной единицей:

```bash
INSTALL_DIR="$PWD"
sudo sed "s#<USER>#$USER#g; s#<INSTALL_DIR>#$INSTALL_DIR#g" deploy/monster-book.service \
  | sudo tee /etc/systemd/system/monster-book.service
sudo sed "s#<USER>#$USER#g; s#<INSTALL_DIR>#$INSTALL_DIR#g" deploy/monster-book.timer \
  | sudo tee /etc/systemd/system/monster-book.timer
sudo systemctl daemon-reload
sudo systemctl enable --now monster-book.timer
systemctl list-timers monster-book --no-pager
cat var/log/book.log
```

Он спрашивает два вопроса каждую минуту `:07`:

1. **Целы ли цепочки** — своя и те, на которые книга ссылается.
   `journal.linked` — утверждение «когда цикл решал, у сборщика было N строк и
   строка N хешировалась в H». Утверждение, которое никто не проверяет, — это
   комментарий, поэтому `--verify` идёт по каждой ссылке и говорит `ok`,
   `edited` (цепочку переписали), `missing` (удалили), `truncated` (обрезали) или
   `broken` (цепочка сама себя не подтверждает). Только `ok` и `unlinked`
   (`цикл сам записал, что не смог прочитать`) считаются нормой.
2. **Совпадает ли кэш с книгой** — `--replay` пересобирает книгу из цепочки той
   же арифметикой, что и торговля, и сравнивает с `var/state.json` поле за полем.

Ненулевой код выхода — и есть сигнализация: `systemctl --failed` показывает
`monster-book.service`, а в `var/log/book.log` лежит строка, где названы оба
числа, которые разошлись. `SuccessExitStatus=` здесь нет намеренно: «почти
сходится» — это не «сходится». Строка `reconcile: rebuilt` в логе — не ошибка, а
пропавший кэш: следующий цикл восстановит его из цепочки.

Плюс сам цикл — раз в час, `:37` (аудит книги — `:07`, чтобы два процесса не
писали в один момент):

```bash
INSTALL_DIR="$PWD"
sudo sed "s#<USER>#$USER#g; s#<INSTALL_DIR>#$INSTALL_DIR#g" deploy/monster-cycle.service \
  | sudo tee /etc/systemd/system/monster-cycle.service
sudo sed "s#<USER>#$USER#g; s#<INSTALL_DIR>#$INSTALL_DIR#g" deploy/monster-cycle.timer \
  | sudo tee /etc/systemd/system/monster-cycle.timer
sudo systemctl daemon-reload
sudo systemctl enable --now monster-cycle.timer
systemctl list-timers monster-cycle --no-pager
cat var/log/cycle.log
```

Цикл читает `var/data`, связывает голову цепочки сборщика и решает. Код выхода —
единственное, что видит таймер, поэтому у каждого факта свой код, а не «2 на
всякий отказ»:

| код | что случилось | что делать |
|---|---|---|
| `0` | цикл прошёл: торговал или отказался и записал почему | ничего |
| `1` | beast говорит «нет»: сработал kill switch, или цепочка не сходится | `--replay`, `var/log/cycle.log`; kill снимается `--reset-kill` — человеком |
| `2` | кэш и цепочка описывают разные книги, или магазин не читается | строка `state.diverged` называет оба числа; кэш убрать — книга восстановится из цепочки |
| `3` | истории меньше окна когерентности: магазин молодой | **ничего**: первые ~2 часа после старта и любая пауза; юнит зелёный |
| `4` | свежий проход сборщика старше `MONSTER_MAX_SNAPSHOT_AGE_S` | `systemctl status monster-capture`: решение на старых данных — решение о прошлом |
| `64` | команда в юните написана неверно | править юнит, а не книгу: ничего не было предпринято |

`SuccessExitStatus=3` — единственное смягчение в этих юнитах, и оно одно: «подождать»
это календарь, а не поломка. Коды `1`, `2`, `4` остаются красными, `systemctl
--failed` называет `monster-cycle.service`, а в `var/log/cycle.log` лежит строка
с числами.

### Первый цикл, который связывает цепочки

Сборщик пишет свою цепочку, книга — свою, и связывает их только цикл: без
`MONSTER_DATA_JOURNAL` он не знает, что рядом есть вторая цепочка, и `--verify`
нечего проверять. В `monster-cycle.service` эта переменная прописана явно.

```bash
cd "$HOME/entrain-core-workspace/monster"
export MONSTER_DATA_JOURNAL=var/journal.capture.jsonl
.venv/bin/python -m monster.cycle --store var/data --interval 60 \
  --journal var/journal.jsonl --state var/state.json
.venv/bin/python -m monster.cycle --verify       # ok (N events), links: k ok
.venv/bin/python -m monster.cycle --replay       # reconcile: ok
```

> **Когда появится первая ссылка.** `journal.linked` пишется перед решением,
> после `data.derived`, поэтому на молодом магазине цикл откажет раньше — с
> кодом `3` и событием `cycle.not_ready` в журнале:
>
> ```
> data: the store holds 0 aligned bar(s) of the 128 the coherence window needs
> (history: 4 symbol(s) excluded for missing bars (… 43/128 …)); a young store is
> waited for, not traded on
> ```
>
> Это отказ молодого магазина, а не поломка: когерентность читается по 128
> закрытым барам, то есть первая ссылка появляется примерно через два часа после
> старта сборщика. `0 aligned` — не ошибка счёта: пока ни один ряд не покрывает
> 90 % окна, выравнивать нечего, а число в скобках (`… 43/128 …`) говорит, сколько
> баров уже есть. До первой ссылки `--verify` печатает только строку цепочки без
> `links:` — это честный ответ «связывать пока нечего», а не пропущенная проверка.



## 5. Ежедневная проверка (2 минуты)

```bash
cd "$HOME/entrain-core-workspace/monster"

# 0. один экран: здоров ли слой данных прямо сейчас.  Восемь чтений
#    (store, freshness, coverage, gaps, journal, readiness, truth, disk),
#    код 0 — всё ok/wait, 1 — есть warn/fail, 2 — магазин вообще не читается.
#    Ничего не пишет: журнал, кэш и замки не трогает, можно запускать при
#    работающем сборщике и в любой момент дня.
.venv/bin/python -m monster.data doctor --store var/data \
  --journal var/journal.capture.jsonl

# 1. вчерашний день целиком воспроизводится из raw
.venv/bin/python -m monster.data audit --store var/data \
  --day "$(date -u -d yesterday +%F)"

# 2. покрытие и дыры за всё время
.venv/bin/python -m monster.data gaps --store var/data --interval 60

# 3. сколько уже собрано
du -sh var/data && ls var/data | tail -3

# 4. сработала ли truth-проверка там, где надо (FP-ворота Фазы 1)
.venv/bin/python -m monster.data truth-report --store var/data \
  --day "$(date -u -d yesterday +%F)"

# 5. какая материальность отклонения честна: свип пола, раз в неделю и перед
#    разговором о пороге z (см. «материальность» ниже)
.venv/bin/python -m monster.data truth-report --store var/data \
  --day "$(date -u -d yesterday +%F)" --min-bps 0,1,2,3

# 6. книга сходится: цепочки целы, кэш — та же книга
.venv/bin/python -m monster.cycle --verify && .venv/bin/python -m monster.cycle --replay

# 7. ворота Фазы 1 целиком, те же данные, один ответ.
#    0 — пройдены, 1 — провалены (дефект данных или доля выше ворот),
#    3 — измерить нельзя (мало данных): это «подожди», а не «всё хорошо».
#    Порог 3 bps и ворота 1 % заморожены в docs/prereg/2026-11-19-truth-materiality-floor.md;
#    hold-out читается пачкой пяти дней (см. §9), по одному дню — только чтобы
#    увидеть, что сбор жив.
.venv/bin/python -m monster.gate --store var/data \
  --day "$(date -u -d yesterday +%F)" \
  --receipt "var/gate-$(date -u -d yesterday +%F).json" \
  --journal var/journal.jsonl

systemctl --failed --no-pager       # пусто — значит ничего не сломано
```

`doctor` отвечает строкой на каждое чтение и одной строкой `[next]` в конце:

```
doctor var/data at 2026-11-17T00:39:00Z: 7 ok, 1 wait
[ok  ] store     var/data: 1 day(s) 2026-11-17, 3 venue(s) (bybit, htx, okx); 2026-11-17: 720 raw, 480 quote, 240 funding
[ok  ] freshness newest quote 0.0s old (limit 180s)
[ok  ] coverage  last 19 min at 60s: 12 series, worst bybit btc-usdt-swap (perp) 20/20 (100.0%)
[ok  ] gaps      none at 60s in the last 19 min
[ok  ] journal   var/journal.capture.jsonl: ok, 40 event(s), last data.captured 0s ago
[wait] readiness 0/128 aligned bar(s) at 60s: the cycle answers 3 (wait) for ~128 more min
[ok  ] truth     4 checked, 0 flagged | 0 unmeasured
[ok  ] disk      var/data: 0.3 MB stored, 865.8 GiB free (floor 1.0)
[next] the store is young — the cycle answers 3 (wait) and the timer stays green: a calendar, not a fault
```

Четыре слова — весь словарь: `ok` — так и должно быть; `wait` — магазин
моложе окна когерентности, это календарь, а не поломка (тот же факт, что код
`3` у цикла), и он **зелёный** для кода выхода; `warn` — факт для ежедневной
проверки, не для трёх ночи (покрытие 96 % у одной биржи, находка truth, ещё не
начатая цепочка сборщика); `fail` — сегодняшние данные вообще не утверждение о
рынке (котировок нет, сборщик встал, цепочка не сходится, диск полон).

Покрытие и дыры считаются по **закрытым** слотам (`--at` минус один интервал):
слот, начавшийся минуту назад, ещё не доставлен, и считать его пропавшим —
значит видеть 98.4 % на здоровом окне. Окно привязано к сетке самих проходов
(последний проход и целое число интервалов назад), а не к настенным часам:
иначе левый край окна попадает между двумя проходами, слот на этом краю
выглядит пропавшим, и здоровый сборщик читается как 75 % только потому, что
`doctor` запустили через 52 секунды после прохода.

Readiness и truth судят **проход**, а не промежуток между проходами — так же,
как цикл: он выводит снимок на самой новой котировке и отдельно спрашивает,
свежий ли этот проход. Поэтому лимитов два, и это два разных вопроса:

* `--max-age` (180 s, `MONSTER_MAX_SNAPSHOT_AGE_S`) — отстал ли сборщик. Проход
  старше лимита — `[fail] readiness` с той же фразой, что код `4` цикла:
  `the cycle refuses: the freshest capture pass is 900s old (limit 180s)`;
* `--quote-max-age` (5 s, `MONSTER_MAX_QUOTE_AGE_S`) — о том ли проходе этот
  снимок. Котировка старше лимита *внутри* прохода выбрасывается из снимка, а
  не усредняется: пара остаётся с двумя биржами, и truth говорит `unmeasured`
  (`2 venue(s) < 3`), а не «чисто».

Без этой развязки `doctor`, запущенный через 52 секунды после прохода при
60-секундном сборе, находил 14 «stale» котировок на здоровом магазине —
находку, которая срабатывает раз в минуту, никто не читает.

Cron (UTC, каждую ночь — по вчерашнему закрытому дню):

```cron
10 0 * * * cd $HOME/entrain-core-workspace/monster && .venv/bin/python -m monster.data audit --store var/data --day $(date -u -d yesterday +\%F) >> var/log/audit.log 2>&1
20 0 * * * cd $HOME/entrain-core-workspace/monster && .venv/bin/python -m monster.data gaps --store var/data --interval 60 >> var/log/gaps.log 2>&1
30 0 * * * cd $HOME/entrain-core-workspace/monster && .venv/bin/python -m monster.data truth-report --store var/data --day $(date -u -d yesterday +\%F) >> var/log/truth.log 2>&1
```

Почему вчерашний, а не сегодняшний: файл сегодняшнего дня в этот момент ещё
открыт на запись, и читать его — значит читать незакрытый gzip-член. Это не
ошибка данных, это ошибка момента.

## 6. Как читать отчёты

```
audit 2026-11-18: 17280/17280 keys agree (17280 raw bodies, 17280 parsed rows)
```

Проверено живыми данными (17.11.2026, три прохода, три биржи):
`audit 2026-11-17: 54/54 keys agree`. Любая строка `only in raw`,
`only normalized` или `mismatched` — стоп-сигнал: значит парсер сегодня читает
тело иначе, чем вчера, и это надо разбирать до продолжения недели.

```
coverage htx btcusdt (spot): 97.9% of 1440 slots
htx btcusdt (spot): 30 missing 60s slot(s) between ...
```

`coverage` — доля слотов, в которых было наблюдение; `gaps` — где именно.
Пропуск — это отсутствие наблюдения, а не ноль: в данных не появится
«цена 0.0», появится дырка с именем.

```
truth 2026-11-18: 1440 pass(es), 5760 cross-section(s) judged · 3 finding(s): 1 persistent, 2 snapped back
false positives: 2/3 judged = 66.7%, 3.47e-04 per cross-section (gate: under 1%)
```

`truth-report` проигрывает проверку по вчерашнему дню и судит каждое
срабатывание по следующему проходу: `persistent` — биржа всё ещё не в толпе
(отказ был прав), `others_followed` — толпа пришла к её цене (она была ранней,
а не ошибочной), `snapped back` — вернулась, а толпа не двигалась (это шум),
`unknown` — судить нечем (конец дня или биржи не было в следующем проходе).
Шумом считается только `snapped back`; два числа — «доля шума среди
проверенных срабатываний» и «шум на кросс-секцию» — отвечают на разные вопросы,
и ворота Фазы 1 (FP < 1%) читаются по обоим. `--max-fp-share 0.01` превращает
это в код выхода для cron.

Число проходов — это проходы **сборщика**, а не строки магазина: биржи ставят
каждый свою метку книги, поэтому проход — это кластер меток в пределах 5 s
(тот же `MONSTER_MAX_QUOTE_AGE_S`), а время прохода — самая новая метка в
кластере. Поэтому строка `1 cross-section(s) judged` рядом с тысячами
«проходов» — не тихий день, а сломанная группировка; `unmeasured` в конце
строки говорит, какие символы прибор **не** смог проверить (например, одна
биржа промолчала в проходе: находки нет, но и «чисто» сказать нельзя).

Срабатывание дешёвое, а отказ — нет. 18 ноября прибор дал 1935 находок на 5421
кросс-секцию, и все они уложились в 0.4–3.3 bps. Это не «биржа врёт», это
арифметика: с тремя биржами масштаб (`1.4826·MAD`) задают две самые близкие, и
биржа в одном bps от пары, сошедшейся до 0.1 bps, получает `z ≈ 10` — z меряет,
насколько биржа одинока, а не насколько велика ложь. Поэтому у находки есть
второе условие — **материальность**: отклонение ниже пола `--min-bps` не находка,
какой бы z ни был. Пол по умолчанию `0` — цикл ведёт себя ровно как раньше, — а
какой пол честный, решает измерение, а не мнение: `--min-bps` принимает список и
печатает воротные цифры для каждого пола.

```
.venv/bin/python -m monster.data truth-report --store var/data \
  --day 2026-11-18 --min-bps 0,1,2,3
  bps floor  findings  snapped back      share  per section  judged
        0.0      1935            46       2.4%     8.49e-03    1934
```

Одна строка на пол: `findings` — сколько отказов прибор сделал, `share` — какая
их доля оказалась шумом, `per section` — шум на кросс-секцию. Порог z не
двигается, пока свип не показал пол, при котором **обе** воротные цифры < 1 %:
пол выбирают по кривой, а не по первому впечатлению. `--max-fp-share` при свипе
сравнивается с худшим полом, поэтому cron-ворота не станут добрее от списка.

Читать магазин можно и во время сбора: если ридер попал в момент, когда
последний gzip-член ещё дописывается, файл перечитывается (несколько попыток
по 50 мс), а не объявляется битым. `Store.reopens` считает такие гонки; если
он растёт сотнями — диск не успевает, стоит уменьшить `--fsync`-частоту, а не
доверие к данным. `audit` всё равно запускай по закрытому дню: у сегодняшнего
дня ещё нет «вчера».

## 7. Остановка, перезапуск, обновление

Остановить: `tmux attach -t monster` → `Ctrl-C` (или `sudo systemctl stop monster-capture`).
Данные уже на диске: каждый проход пишется с `fsync`.

Перезапуск (например, после обновления кода):

```bash
cd "$HOME/entrain-core-workspace/monster"
git pull                                         # или rsync заново
.venv/bin/python -m monster.data probe --live     # форма ещё та?
.venv/bin/python -m monster.cycle --verify        # журнал не правили?
# если менялись deploy/* — переустановить юниты из §4: daemon-reload читает
# /etc/systemd/system, а не ваш checkout, и сам файлы не переписывает.
# затем снова команда из §4
```

Данные переживают обновление кода, но **не** переживают `git clean -fdx`:
`var/` в `.gitignore`, поэтому git её не тронет, а `clean -x` — тронет.

## 8. Если что-то не так

| Симптом в `probe` / логе | Что это значит | Что делать |
|---|---|---|
| `HTTP Error 403` | биржа не любит наш `User-Agent` или IP-регион | `curl` с того же VPS; если 403 и там — вопрос к сети VPS, не к коду |
| `HTTP Error 429` | слишком часто | `--interval 90` (покрытие считается по слотам, а не по запросам) |
| `HTTP Error 451` | гео-ограничение | этот эндпоинт с VPS недоступен; писать Архитектору |
| `the answer is not JSON` | вместо JSON — HTML (прокси, captive portal) | `curl` тот же URL; если HTML — виноват прокси/сеть |
| `timeout` в каждом проходе | медленная сеть | `--timeout 8 --workers 4` |
| `no day to audit` | день ещё не создан (или опечатка в `--day`) | `ls var/data` |
| `BadGzipFile` при `audit` | читается сегодняшний, ещё открытый файл | аудитить вчерашний день |
| `17 reading(s)` каждый раз | один эндпоинт отказал | в строке будет имя и причина — это и есть ценность |
| `System clock synchronized: no` | часы уплыли | §1, и только потом продолжать |
| `links: 1 of 1 do not hold` в `--verify` | цепочку переписали после того, как книга её прочитала | не «чинить» книгу: строка `edited`/`missing` называет файл; без бэкапа неделя под вопросом |
| `reconcile: diverged` в `--replay` | кэш `var/state.json` и цепочка описывают разные книги | цикл сам откажет (код 2); кэш удаляем — он восстановится из цепочки (`--replay --write-state`) |
| `reconcile: rebuilt` в логе `book.log` | кэш пропал (не расхождение) | ничего не делать: следующий цикл пересоберёт его из цепочки |
| `cycle.not_ready` / код `3` в `cycle.log` | магазин моложе окна когерентности (128 закрытых баров) | не ошибка: юнит зелёный (`SuccessExitStatus=3`), решение появится через ~2 часа; число `… 43/128 …` в строке говорит, сколько уже есть |
| `doctor` код `1` со строкой `[fail] freshness` | сборщик не пишет свежие котировки (в строке — возраст и лимит) | `systemctl status monster-capture`; не искать причину в магазине, пока сборщик стоит |
| `doctor` код `2` | магазин не читается вовсе (нет прав, не каталог, битый день) | `ls -ld var/data`; это ответ «вопрос не задан», а не «данные плохи» |
| `doctor` печатает `[wait] readiness` | магазин моложе окна когерентности — цикла ещё не было | ничего не делать: это код `3` цикла, зелёный по расписанию |
| `doctor` строка `[warn] truth` | truth-проверка что-то нашла (или не смогла измерить) | `truth-report --day <вчера>` называет пару и число; в узком рынке z раздут — смотри ещё и bps, и свип пола `--min-bps 0,1,2,3` |
| `doctor` строка `[warn] truth … unmeasured: 2 venue(s) < 3` | одна биржа не попала в проход (котировка старше `--quote-max-age`) | это «проверить не смог», а не «чисто»: смотри `[warn] coverage` — какая биржа молчит и сколько слотов потеряно |
| `doctor` строка `[fail] readiness the cycle refuses` | снимок пришёл из прохода старше `--max-age` | то же, что код `4` цикла: `systemctl status monster-capture`; на этих данных цикл не решит |
| `the freshest capture pass is N s old` (код `4`) | сборщик отстал сильнее `MONSTER_MAX_SNAPSHOT_AGE_S` | `systemctl status monster-capture`; решение на старых данных — это решение о прошлом |

Важно: **одна отказавшая биржа не останавливает сбор**. В логе будет
`17 reading(s)` и имя отказавшего эндпоинта; остальные 17 пишутся. Это
задумано: неделя данных без OKX полезнее, чем никакой недели.

## 9. Что прислать через 7 дней

```bash
cd "$HOME/entrain-core-workspace/monster"
.venv/bin/python -m monster.data probe --live > var/probe.txt
.venv/bin/python -m monster.data gaps --store var/data --interval 60 > var/gaps.txt
for d in $(ls var/data); do
  .venv/bin/python -m monster.data audit --store var/data --day "$d" >> var/audit.txt
done
.venv/bin/python -m monster.cycle --verify > var/journal.txt
.venv/bin/python -m monster.cycle --verify --journal var/journal.capture.jsonl >> var/journal.txt
.venv/bin/python -m monster.cycle --replay >> var/journal.txt
du -sh var/data >> var/journal.txt
cp var/log/book.log var/book.txt 2>/dev/null || true
cp var/log/cycle.log var/cycle.txt 2>/dev/null || true
for d in $(ls var/data); do
  .venv/bin/python -m monster.data truth-report --store var/data --day "$d" --json >> var/truth.jsonl
done
# ворота Фазы 1: один ответ на всю неделю и его артефакт.
# Дни калибровки и утечки сюда не входят — только окно из предрегистрации.
.venv/bin/python -m monster.gate --store var/data \
  --day 2026-11-20 2026-11-21 2026-11-22 2026-11-23 2026-11-24 \
  --receipt var/gate-phase1.json --journal var/journal.jsonl > var/gate.txt
```

Эти файлы — `probe.txt`, `gaps.txt`, `audit.txt`, `journal.txt`, `truth.jsonl`,
`gate.txt` + `gate-phase1.json` (вердикт ворот и его receipt),
`book.txt` (ежечасный аудит книги: `ok`, `links: k ok`, `reconcile: ok` или
строка с обоими разошедшимися числами) и `cycle.txt` (ежечасный цикл: `ok`,
отказ с кодом и причиной или `cycle.not_ready` с числом баров) — и есть ответ на
вопрос «прошла ли Фаза 1 свои ворота». Ничего не надо пересказывать словами:
числа либо сходятся, либо нет.  Если `gate.txt` говорит `unmeasurable` — это
не «почти прошли», а «на этих данных ворота не заданы»: смотри, какое из
условий (`coverage`, `audit`, `sample`, `truth.decision`) не набрало выборки.

`gate-phase1.json` — receipt, а не лог: в нём `policy` (все числа ворот, включая
`min_bps`, `min_venues`, `z`, `max_age_s`, `max_deviation_bps`) и `classifier`
(`snap_fraction`, `snap_floor_bps`, `follow_fraction`, `fp_verdict`).  Поэтому
вердикт можно перечитать через месяц и получить тот же ответ, не сверяясь с
историей коммитов.  Сам `monster.gate` **не** стоит в таймере: ворота читают
окно, которое должно закончиться, а таймер, сработавший в середине окна, напишет
`unmeasurable` и создаст видимость результата.

## 9a. Пока Rust-зверь остановлен

Пока `ENRG-AI` не запущен, `monster.cycle` и `monster.book` работать не могут (им
нужны фиды), а их строки в журнале будут отказами, а не книгой:

```bash
sudo systemctl disable --now monster-cycle.timer monster-book.timer
systemctl is-active monster-capture.service monster-probe.timer monster-audit.timer
```

На время окна нужны **только** сбор и аудит данных: `capture` (пишет
`var/data`, `--count 10080` — неделя проходов), `probe` (ежечасно) и `audit`
(ежедневно в 00:10 UTC за вчерашний день).  Если `capture` перезапускается
посреди дня, в этот день появляется пропуск; `Restart=always` +
`RestartSec=10` дают не больше одного пропущенного слота на рестарт, а ворота
требуют 1380 из 1441 слота — то есть запас есть ровно на такие рестарты, но не на
часовую дыру.  Окно закрывается в 00:00 UTC 25.11; аудит за 24.11 запускается в
00:10, поэтому ворота имеют смысл не раньше этого времени (внутри `monster.gate`
аудит считается сам, но `var/audit.txt` тогда ещё не содержит 24.11).

## 10. Чего не делать

* не запускать `capture` от `root` — незачем; ключей у пакета нет;
* не класть `.env` с ключами в этот каталог: сбор их не читает, а лишняя копия
  секретов рядом с данными — риск без выгоды;
* не менять `--interval` посреди недели: `gaps` считает по одному интервалу;
* не менять **парсеры и нормализацию** до конца недели (см. §10 в
  `docs/MONSTER.md`): `audit` сравнивает сегодняшний парсер со вчерашними
  строками, и любое новое поле превращает честный день в `mismatched`;
* не «поправлять» файлы в `var/data` руками: `audit` поймает, а доверие к
  неделе обнулится;
* не удалять `var/journal.jsonl` и `var/journal.capture.jsonl` — это
  единственная защита от тихой правки истории (проверяется `monster.cycle
  --verify`); `--replay` показывает, что книга в кэше — та же книга, что в
  цепочке, и расхождение цикла — это отказ, а не повод «подправить» кэш.

