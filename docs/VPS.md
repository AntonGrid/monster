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

> Команды `probe`, `capture`, `audit`, `gaps`, `derive` не импортируют
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

```bash
sudo sed "s#<USER>#$USER#g; s#<HOME>#$HOME#g" \
  deploy/monster-capture.service | sudo tee /etc/systemd/system/monster-capture.service
sudo systemctl daemon-reload
sudo systemctl enable --now monster-capture
systemctl status monster-capture --no-pager
journalctl -u monster-capture -n 20 --no-pager
```

`Restart=always` означает, что падение стоит секунд, а не недели. В tmux
такого нет — если выбираешь tmux, проверяй `tmux ls` хотя бы раз в день.

Плюс ежедневный `audit` таймером (00:10 UTC, по вчерашнему дню):

```bash
sudo sed "s#<USER>#$USER#g; s#<HOME>#$HOME#g" deploy/monster-audit.service \
  | sudo tee /etc/systemd/system/monster-audit.service
sudo sed "s#<USER>#$USER#g; s#<HOME>#$HOME#g" deploy/monster-audit.timer \
  | sudo tee /etc/systemd/system/monster-audit.timer
sudo systemctl daemon-reload
sudo systemctl enable --now monster-audit.timer
systemctl list-timers monster-audit --no-pager
```

Плюс часовой `probe` — сторож формы ответов, а не данных:

```bash
sudo sed "s#<USER>#$USER#g; s#<HOME>#$HOME#g" deploy/monster-probe.service \
  | sudo tee /etc/systemd/system/monster-probe.service
sudo sed "s#<USER>#$USER#g; s#<HOME>#$HOME#g" deploy/monster-probe.timer \
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
sudo sed "s#<USER>#$USER#g; s#<HOME>#$HOME#g" deploy/monster-book.service \
  | sudo tee /etc/systemd/system/monster-book.service
sudo sed "s#<USER>#$USER#g; s#<HOME>#$HOME#g" deploy/monster-book.timer \
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

### Первый цикл, который связывает цепочки

Сборщик пишет свою цепочку, книга — свою, и связывает их только цикл: без
`MONSTER_DATA_JOURNAL` он не знает, что рядом есть вторая цепочка, и `--verify`
нечего проверять.

```bash
cd "$HOME/entrain-core-workspace/monster"
export MONSTER_DATA_JOURNAL=var/journal.capture.jsonl
.venv/bin/python -m monster.cycle --store var/data --interval 60 \
  --journal var/journal.jsonl --state var/state.json
.venv/bin/python -m monster.cycle --verify       # ok (N events), links: k ok
.venv/bin/python -m monster.cycle --replay       # reconcile: ok
```

> **Когда появится первая ссылка.** `journal.linked` пишется перед решением,
> после `data.derived`, поэтому на молодом магазине цикл откажет раньше:
> `data: history: 43 of 128 bars fit before ...`. Это отказ молодого магазина,
> а не поломка — когерентность читается по 128 закрытым минутам, то есть первая
> ссылка появляется примерно через два часа после старта сборщика. До тех пор
> `--verify` печатает только строку цепочки без `links:` — это честный ответ
> «связывать пока нечего», а не пропущенная проверка.



## 5. Ежедневная проверка (2 минуты)

```bash
cd "$HOME/entrain-core-workspace/monster"

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

# 5. книга сходится: цепочки целы, кэш — та же книга
.venv/bin/python -m monster.cycle --verify && .venv/bin/python -m monster.cycle --replay
systemctl --failed --no-pager       # пусто — значит ничего не сломано
```

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
| `history: N of 128 bars fit` | магазин моложе окна когерентности | не ошибка; решение появится, когда наберётся 128 закрытых минут |
| `the freshest capture pass is N s old` | сборщик отстал сильнее `MONSTER_MAX_SNAPSHOT_AGE_S` | `systemctl status monster-capture`; решение на старых данных — это решение о прошлом |

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
for d in $(ls var/data); do
  .venv/bin/python -m monster.data truth-report --store var/data --day "$d" --json >> var/truth.jsonl
done
```

Эти файлы — `probe.txt`, `gaps.txt`, `audit.txt`, `journal.txt`, `truth.jsonl`
и `book.txt` (ежечасный аудит книги: `ok`, `links: k ok`, `reconcile: ok` или
строка с обоими разошедшимися числами) — и есть ответ на вопрос «прошла ли Фаза
1 свои ворота». Ничего не надо пересказывать словами: числа либо сходятся, либо
нет.

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

