# Развёртывание на офисном сервере

## Что проверено 19 ноября 2026

Аудит выполнен через SSH без изменения приложений. Фактическая конфигурация:

| Компонент | Фактическое состояние |
|---|---|
| Репозиторий обновления | `/opt/gab`, `main`, commit `4957ab6e3032588de2edd5a600687b26c8fde1d7` на момент аудита |
| Старый checkout | `/opt/gab-review`, ветка `server-sync-20261119_004804`; для нового deploy не используется |
| Compose | `/root/ai-stack/docker-compose.yml`, проект `ai-stack` |
| OCR | Compose service `gab_ocr`, build context `/opt/gab/services/gab-ocr`, Dockerfile `Dockerfile` |
| OCR runtime | версия `0.4.4`, Tesseract `5.5.0`, `rus` и `eng`; HTTP health 200 |
| CRM | отдельный контейнер `gab-review`, вне Compose; текущий образ уже `gab-review:main-4957ab6` |
| CRM публикация | `192.168.1.99:8091` → `8000/tcp` |
| CRM данные | bind `/opt/gab-review-data:/srv/gab-review-data`, сеть `ai-stack_ai_net` |
| PostgreSQL | `ai-stack-postgres_ai-1`, `gab_crm`, пользователь `n8n_user` |
| n8n | `ai-stack-n8n_ai-1`, версия `2.42.7`; GAB 02 выключен, 27 узлов |
| OCR credential | `GAB OCR API`, ID `6Wb6WQE64aBvpr95` |
| Миграция | `worker_token`, `manual_overrides`, `edit_version` уже присутствуют; повторная миграция всё равно проверяется |
| Шифрование backup | `age` на сервере отсутствует; предусмотрена явная первоначальная настройка |

Это отличается от исходного предположения, что CRM и миграция ещё совсем не обновлялись. Deploy всегда повторно проверяет текущее состояние, а параметры контейнера читает из Docker inspect, а не из старого имени образа.

Рабочий экспорт GAB 02 взят за основу файла `n8n/workflows/GAB02InvImp20261.json`. Сохранены 27 узлов, credential references и реальные ветки PDF/Word/XLS/XLSX. Добавлена передача `worker_token` из claim в оба сохранения строк и обработку ошибки, необходимая текущему CRM API. Сохранена защита от присвоения одного восстановленного со всей страницы ИНН нескольким компаниям. Метаданные владельца проекта исключены из Git; секреты credentials не экспортировались в исходный код.

Это сохраняет существующую серверную Excel-ветку `extractFromFile`; многолистность универсального OCR-парсера проверена отдельно, но этот экспорт не переводит Excel на новый OCR endpoint. Поддержка CSV/изображений/PPTX самим OCR не означает, что старый 27-узловой workflow уже маршрутизирует эти форматы. Это отдельная доработка, не повод объявлять их сквозную обработку проверенной.

## Одна команда после git pull

Войдите на сервер, выполните `sudo -i`, затем обновите **`/opt/gab`**, не старый `/opt/gab-review`:

```bash
cd /opt/gab
git pull --ff-only
bash scripts/deploy-all.sh --bootstrap-backup
```

Последняя строка — единый запуск. Флаг `--bootstrap-backup` разрешает установить `age` из настроенных системных APT-репозиториев, если его нет, и создать ключ, если он отсутствует. Повторный запуск не перезаписывает существующий ключ. Другие Python-пакеты на сервер для самих deploy-скриптов не требуются: используется Python standard library.

Проверка без развёртывания:

```bash
cd /opt/gab
bash scripts/deploy-all.sh --check
```

`--check` проверяет ветку/commit/чистоту, исходники, Docker, Compose context, SQL-доступ, состояние очереди, CLI и workflow. Не устанавливает age, не создаёт ключ, не собирает образ, не применяет миграцию, не перезапускает и не публикует workflow. Недостающую первоначальную настройку backup выводит отдельным сообщением. Нужны root/Docker-права; используется общий deployment lock.

## Что делает deploy-all

1. Требует чистый `main` и совпадение `HEAD` с `origin/main`. `EXPECTED_COMMIT` необязателен: по умолчанию фиксируется текущий полный HEAD; если переменная задана, он обязан совпасть.
2. Проверяет Python compile без записи pycache, JavaScript/shell syntax и JavaScript code nodes workflow. Для GAB 02 проверяет ровно 27 известных узлов, ссылки веток и OCR credential. Почтовые интеграции, неизвестные HTTP destinations и `/confirm` запрещены.
3. Проверяет реальные работающие контейнеры, Compose context, PostgreSQL и n8n `2.42.7`. Если есть незавершённые GAB 02 executions или пакеты `processing`, останавливается до изменений. Не сбрасывает очередь автоматически.
4. Проверяет OCR health, русские/английские языки и endpoints. Исправный OCR **не пересобирается**.
5. Создаёт и проверяет зашифрованный backup. Проверяет `pg_restore --list` и полную успешную расшифровку архива. Ошибка backup блокирует любые изменения приложений.
6. Собирает CRM из текущего корневого Dockerfile в новый commit-tag; исходный образ не перетегируется.
7. Снимает с публикации только GAB 02, ждёт применения изменения работающим n8n и повторно проверяет отсутствие обработок. Другие workflow не останавливает.
8. Применяет `20261119_import_editor.sql` через `psql -X -v ON_ERROR_STOP=1`. В миграции локальные lock/statement timeouts; существующие данные не удаляются. Проверяет новые поля.
9. Останавливает прежний `gab-review`, переименовывает его в `gab-review-rollback-<release>`, сохраняет его для возврата. Создаёт новый контейнер через локальный Docker Engine API с прежними Config/HostConfig и сетями. Переносит env, port bindings, bind/named/anonymous volumes, user, command, entrypoint, limits, restart/security/logging settings и настроенные aliases/static IP. Динамические endpoint IDs/IP не копируются. Старые network attachments освобождаются перед созданием и восстанавливаются при откате. После запуска сверяет критичные настройки с прежними.
10. Проверяет защищённый CRM `/health`, чтение импортов, CSS/JS и наличие editor/match-preview/confirm routes. Проверки не редактируют production-строки и не подтверждают импорт.
11. Импортирует файл ровно с одним ID `GAB02InvImp20261` через `n8n import:workflow --input=...`. Публикует новую версию через `n8n publish:workflow --id=GAB02InvImp20261 --versionId=...`. Не импортирует credentials и не использует `--all` для изменений.
12. Проверяет не только `activeVersionId` в БД: ждёт фактической `workflow_published_version`, завершения publication outbox и статуса `activated` у schedule trigger новой версии. Проверяет n8n readiness и fingerprints других workflow.
13. Выводит итог, commit, путь backup, журнал и точную команду отката.

В n8n 2.42.7 CLI меняет БД, а включённый workflow publication service/reconciler применяет её к runtime. Проверены команды установленного CLI и исходники точных npm-пакетов `n8n@2.42.7`, `@n8n/config@2.40.2`, `@n8n/db@1.42.5`. Для выключенного publication service deploy заранее отказывает, вместо общего рестарта n8n. Общая документация механизма: [n8n Server CLI](https://docs.n8n.io/hosting/cli-commands/).

Публикация включает существующее расписание GAB 02. Оно может начать распознавание ожидающих документов. Узлов подтверждения CRM и отправки писем в этом workflow нет; карточки создаются только по ручному подтверждению сотрудника.

## Конфигурация без цепочки недостающих env

Проверенные серверные значения находятся в `scripts/deploy_config.py`. На этом сервере задавать `COMPOSE_FILE`, `BACKUP_DIR`, `AGE_RECIPIENT` и остальные переменные по одной не нужно.

При необходимости скопируйте `scripts/deploy-config.example.json` в `/etc/gab-invest/deploy.json`, владелец root, права 0600. JSON должен содержать только нужные переопределения; неизвестные ключи отклоняются. Альтернативный путь задаётся `GAB_DEPLOY_CONFIG`. Приватная конфигурация, env и пароли в Git не помещаются.

По умолчанию:

- Архивы: `/var/backups/gab-invest`, каталог 0700, файлы 0600.
- Журналы развёртывания: `/var/lib/gab-invest/deployments/<release>/deployment.json`, каталог 0700.
- AGE private identity: `/root/.config/gab-invest/backup.agekey`, каталог 0700, файл 0600.
- Public recipient вычисляется из identity через `age-keygen -y`; ошибку не нужно исправлять ручным копированием приватного ключа в переменную окружения.

`extra_config_paths` позволяет включить дополнительные файлы настроек, не упомянутые в Compose или runtime inspect. `publication_timeout_seconds=180` задаёт ограничение ожидания публикации. Пароли CRM берутся внутри работающего контейнера, не попадают в аргументы команд и вывод.

## Backup и ключ

В архив входят:

- custom-format dump `gab_crm`;
- полный экспорт workflow для резервного восстановления и отдельный GAB 02; при наличии — прежняя опубликованная версия отдельно от draft;
- credentials n8n **в зашифрованном виде**, без `--decrypted`;
- Docker inspect CRM/OCR/n8n/PostgreSQL, включая параметры запуска и env, только внутри зашифрованного архива;
- прежние Docker images CRM/OCR;
- документы из реальных CRM bind mounts;
- полный Compose, соседний и репозиторный `.env`, приватный deploy config, n8n settings/config и настроенные дополнительные пути;
- карта исходных путей, commit и release metadata.

Env-based `N8N_ENCRYPTION_KEY` сохраняется в зашифрованном Docker inspect; key из n8n config сохраняется вместе с конфигурацией. Без него encrypted credentials не восстановить. Архив authenticates/decrypts успешно до переключения контейнеров. Небольшой checksum-файл рядом содержит SHA-256 encrypted archive, а не исходных персональных документов.

Созданный private AGE key **сразу скопируйте в отдельное защищённое хранилище вне сервера**. Не публикуйте его в GitHub и не храните единственную recovery-копию на том же диске, что архивы. После такой копии дальнейшие deploy не требуют bootstrap-флага:

```bash
bash scripts/deploy-all.sh
```

Отдельный backup: `bash scripts/backup.sh --bootstrap-backup` при первом запуске, далее `bash scripts/backup.sh`. Backup не публикует и не запускает workflow. Dump БД транзакционно согласован, но dump и файловый архив не являются общей файловой snapshot: для полного disaster recovery нужен период без пользовательских изменений. Скрипт не выдумывает глобальный maintenance mode и не выполняет `docker compose down`.

Расписание, ротация и offsite-хранилище не настраиваются этим deploy. Старые остановленные rollback-контейнеры и архивы не удаляются автоматически; свободное место следует контролировать.

## Отдельные компоненты

```bash
bash scripts/deploy-crm.sh
bash scripts/deploy-ocr.sh                  # только health, без rebuild/restart/backup bootstrap
bash scripts/deploy-ocr.sh --rebuild-ocr    # явное разрешение пересборки только OCR
bash scripts/deploy-workflow.sh
```

Обновлять несовместимые CRM/workflow следует через `deploy-all`. Отдельный CRM/OCR deploy не активирует старый workflow без worker token. Перед отдельной публикацией проверяются уже установленные CRM editor API. OCR rebuild допустим только с правильным Compose context; используется `docker compose build gab_ocr`, остальные сервисы не собираются. Конфигурация/тома PostgreSQL и n8n не меняются.

## Откат

При ошибке после начала переключения выполняется компенсация: GAB 02 снимается с публикации, проверяется отсутствие выполняющихся обработок, прежние контейнеры возвращаются с исходными именами и сетями, восстанавливаются прежние draft и published state GAB 02. Если началась обработка, автоматический откат останавливается и требует оператора: нельзя переключать протокол CRM посреди импорта.

Журнал сохраняется атомарно после значимых шагов. Ручной повтор отката:

```bash
bash scripts/rollback.sh /var/lib/gab-invest/deployments/RELEASE/deployment.json
```

Нужны сохранённый AGE identity, проверенный архив и прежние остановленные контейнеры. Скрипт удаляет только новый контейнер с matching release label, **без `-v`**, и не удаляет данные томов. Если старый контейнер уже удалён сторонними действиями, скрипт останавливается: в архиве есть его inspect и image для операторского восстановления. Автоматически удалять/восстанавливать production-БД скрипт не будет.

SQL-миграция аддитивная: при откате образа новые поля и аудит остаются. Восстановление dump допускается только отдельно в новую БД после согласования. Обрыв питания/SIGKILL может потребовать ручного запуска rollback по журналу; повторный deployment параллельно запрещён lock-файлом.

## Проверки этой версии

- Python compile, JavaScript и shell syntax; проверка code nodes и всех основных связей 27-узлового workflow.
- API/editor tests на новой локальной PostgreSQL и искусственных записях, включая match-preview, правки, аудит, сохранение после OCR, кандидатов ИНН/email/телефон, ручное подтверждение и два одновременных подтверждения.
- Новая миграция повторно применена к заполненной тестовой БД: записи/аудит совпадают, OID существующих таблиц/индекса не меняются.
- Форматы PDF с текстом, DOCX, XLS/XLSX с несколькими листами, CSV, текст PPTX; повреждённые форматы.
- Отдельно в действующем OCR на искусственных файлах: настоящий binary DOC, изображение и сканированный PDF — успешно. Никаких CRM upload/confirm в production при этих проверках не было.
- Локальный браузер с искусственной БД: кнопка импорта, CSS, таблица, сохранение правки/история, match-preview, «Компании → Добавить» без `config.title` и без console errors.
- Реальный локальный roundtrip backup: pg_dump → pg_restore --list → age encrypt → authenticated decrypt → recovery metadata; неверный ключ отклоняется. Docker-вызовы в этом тесте заменены, PostgreSQL и age настоящие.
- Изолированные deployment tests: сохранение runtime-параметров и нескольких сетей, static IP и anonymous volume, возврат после create failure, запрет удаления чужого контейнера, failure compensation после каждого этапа, публикация считается успешной только после runtime activation.

Полный локальный прогон: **38 тестов, 37 прошли, 1 пропущен** (локально нет Tesseract; соответствующие изображение/скан и DOC отдельно успешно проверены на рабочем OCR).

Production deployment, реальное переключение контейнера и создание production-backup **не запускались**. Прогон Docker swap/rollback выполнен на in-memory Docker double; CLI-флаги и фактический сервер проверены отдельно. Первый запуск остаётся операторским, по команде выше.
