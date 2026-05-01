# AI_PLAYBOOK — Мультиагентная методология разработки

Разобранная методология из репозитория Pavel Molyanov (molyanov-ai-dev).
Используется для работы с Claude Code CLI + мультиагентная оркестрация.

---

## Суть

Вся разработка строится по принципу **spec-driven pipeline**: сначала пишем спецификации, потом код. Никакого кода без утверждённого плана.

Каждый этап проверяется автоматическими агентами-валидаторами. Максимум 3 попытки исправить что-то на каждом этапе, если не получается — эскалация к пользователю.

Код пишется через TDD: сначала тесты, потом реализация.

---

## Пайплайн — 5 шагов от идеи до продакшена

```
Идея
  → /new-user-spec     (что делаем, на русском)
  → /new-tech-spec     (как делаем, архитектура)
  → /decompose-tech-spec (разбивка на задачи)
  → /do-feature или /do-task (реализация)
  → /done              (обновление доков, архив)
```

### Шаг 1. User Spec — `/new-user-spec`

Скилл: `user-spec-planning`

Агент проводит интервью в 3 цикла:
1. Общие вопросы — что хотим, зачем, для кого
2. С учётом кода — агент изучил проект и уточняет по интеграции и паттернам
3. Edge cases — граничные случаи, что если сломается

После интервью агент `interview-completeness-checker` проверяет что ничего не пропустили.

Создаётся `user-spec.md` — на русском, для человека, с acceptance criteria.

Параллельно работают 2 валидатора (до 3 итераций исправлений):
- `userspec-quality-validator` — структура документа, тестируемость критериев
- `userspec-adequacy-validator` — осуществимость, нет ли переусложнения

Пользователь читает → утверждает.

**Результат:** `work/{feature}/user-spec.md` (status: approved)

---

### Шаг 2. Tech Spec — `/new-tech-spec`

Скилл: `tech-spec-planning`

Пишется на английском — это документ для агента. Агент:
- Исследует кодовую базу через агента `code-researcher`
- Проверяет зависимости
- Использует Context7 MCP для актуальной документации библиотек
- Задаёт технические уточняющие вопросы

Содержание tech-spec:
- Архитектура и компоненты
- Ключевые технические решения (каждое с обоснованием)
- Shared Resources — тяжёлые объекты (ML-модели, DB-пулы)
- Стратегия тестирования
- План реализации по волнам (waves)
- В конце всегда: Audit Wave + Final Wave (QA + деплой)

Параллельно 5 валидаторов (до 3 итераций):
- `skeptic` — ищет миражи: несуществующие файлы, функции, API
- `completeness-validator` — двустороняя трассировка: все ли требования из user-spec покрыты
- `security-auditor` — OWASP Top 10, архитектурные решения
- `test-reviewer` — адекватность стратегии тестирования
- `tech-spec-validator` — соответствие шаблону, качество задач

**Результат:** `work/{feature}/tech-spec.md` (status: approved)

---

### Шаг 3. Декомпозиция — `/decompose-tech-spec`

Скилл: `task-decomposition`

Для каждой задачи из tech-spec параллельно создаётся отдельный файл агентом `task-creator`.

Каждый task-файл содержит:
- `status`, `wave`, `depends_on`, `skills`, `reviewers` (в YAML frontmatter)
- Description — что делать
- TDD Anchor — какие тесты писать первыми
- Acceptance Criteria
- Context Files — список файлов которые нужно прочитать
- Verify-smoke — автоматическая проверка (curl, python -c, docker)
- Verify-user — ручная проверка пользователем

2 валидатора (до 3 итераций):
- `task-validator` — соответствие шаблону, качество описания
- `reality-checker` — существуют ли указанные файлы и функции в реальном коде

Потом перекрёстная проверка всех задач вместе (до 2 дополнительных итераций) — ищет конфликты по общим файлам и скрытые зависимости.

**Результат:** `work/{feature}/tasks/*.md`

---

### Шаг 4. Реализация — два режима

#### Режим А: `/do-task` — одна задача

Подходит для: ручного контроля, отладки, сложных задач.

1. Читает task-файл и все Context Files
2. Загружает скиллы из frontmatter задачи (обычно `code-writing`)
3. TDD: сначала тесты, потом код
4. Коммит после того как тесты прошли
5. Запускает ревьюеров из frontmatter (до 3 раундов)
6. Коммит после каждого раунда исправлений
7. Пишет запись в `decisions.md`, обновляет статус задачи

#### Режим Б: `/do-feature` — вся фича командой

Скилл: `feature-execution`

Тимлид — чистый диспетчер. Запрещено: писать код, запускать билды, дебажить.

**Фаза 1: Инициализация**
- Проверяет `checkpoint.yml` — если `last_completed_wave > 0`, значит возобновление после компакции контекста. Пропускает выполненные волны.
- Читает tech-spec и frontmatter всех задач
- Строит план выполнения по волнам
- Показывает план пользователю, ждёт одобрения
- Создаёт команду через TeamCreate
- Инициализирует `checkpoint.yml`

**Фаза 2: Выполнение волны**

Для каждой задачи в волне тимлид спавнит:

**Teammate** (general-purpose, модель opus):
- Читает task-файл
- Загружает скиллы из frontmatter
- Работает по TDD
- Коммитит код после прохождения тестов
- Отправляет diff ревьюерам
- После каждого раунда исправлений — коммит
- Пишет запись в decisions.md
- Сообщает тимлиду "Task N complete"

**Reviewers** (general-purpose, модель sonnet) для каждого ревьюера из frontmatter задачи:
- Загружает свою методологию через Skill
- Читает user-spec, tech-spec, task
- Ждёт diff от teammate
- Пишет JSON-отчёт в `logs/working/task-N/reviewer-roundN.json`
- Отправляет путь к отчёту teammate
- Максимум 3 раунда

Маппинг ревьюер → скилл:
```
code-reviewer       → code-reviewing
security-auditor    → security-auditor
test-reviewer       → test-master
prompt-reviewer     → prompt-master
deploy-reviewer     → deploy-pipeline
infrastructure-reviewer → infrastructure-setup
skill-checker       → skill-master
documentation-reviewer  → documentation-writing
```

**Фаза 3: Переход между волнами**
- Проверяет что все задачи волны отчитались
- Обновляет статусы в frontmatter задач
- Коммит статусов (код уже закоммичен teammates)
- Обновляет checkpoint.yml
- Переходит к следующей волне

**Audit Wave** (всегда последняя перед финалом):
Три агента параллельно читают ВЕСЬ код фичи целиком (не диффы):
- `code-reviewer` — читает все изменённые файлы, пишет отчёт
- `security-auditor` — то же, с фокусом на безопасность
- `test-reviewer` — то же, с фокусом на тесты

Если нашли проблемы → тимлид спавнит fixer-агента с теми же ревьюерами (max 3 раунда). Если не решилось → эскалация.

**Final Wave**: pre-deploy QA → deploy → post-deploy QA (если применимо).

**Фаза 4: Ревью пользователем**
- Тимлид показывает результаты
- Описывает что проверить вручную
- Если проблемы → ad-hoc агент для исправления
- После одобрения: команда распускается, checkpoint.yml удаляется

**Ad-hoc агенты** (для незапланированных задач):
```
Код         → skill: code-writing,       reviewers: code-reviewer, security-auditor, test-reviewer
Промпты     → skill: prompt-master,      reviewers: prompt-reviewer
Скиллы      → skill: skill-master,       reviewers: skill-checker
Деплой      → skill: deploy-pipeline,    reviewers: deploy-reviewer
Инфра       → skill: infrastructure-setup, reviewers: infrastructure-reviewer, security-auditor
Остальное   → без скилла и ревьюеров
```

**Эскалация** (после 3 неудачных итераций):
1. Стоп всех работ на задаче
2. Отчёт пользователю: что сломалось, что пробовали, что осталось
3. Запись в decisions.md
4. Коммит с пометкой escalate
5. Ждём решения

---

### Шаг 5. Финализация — `/done`

- Читает user-spec, tech-spec, decisions.md
- Обновляет Project Knowledge (architecture.md, patterns.md, deployment.md)
- Переносит `work/{feature}/` → `work/completed/{feature}/`
- Коммит

---

## Project Knowledge — база знаний проекта

Вся документация живёт в `.claude/skills/project-knowledge/references/`.
CLAUDE.md — минимальный, только ссылка на эту систему.

| Файл | Что внутри |
|------|-----------|
| `project.md` | Название, описание, аудитория, ключевые фичи, что НЕ делаем |
| `architecture.md` | Стек, структура папок, зависимости, внешние интеграции, модель данных |
| `patterns.md` | Конвенции кода, git workflow, стратегия тестирования, бизнес-правила |
| `deployment.md` | Платформа, env vars, CI/CD, мониторинг, rollback |
| `ux-guidelines.md` | UI-язык, тон, глоссарий (опционально, для UI-проектов) |

Создаются командой `/init-project-knowledge` через скилл `project-planning`.
Обновляются командой `/done` через скилл `documentation-writing`.

Агент читает только то, что нужно для текущей задачи — не всё сразу.

---

## Структура файлов при работе над фичей

```
work/
└── {feature-name}/
    ├── user-spec.md          # Требования (русский, для человека)
    ├── tech-spec.md          # Архитектура (английский, для агента)
    ├── decisions.md          # Решения принятые в ходе работы
    ├── tasks/
    │   ├── 1.md
    │   ├── 2.md
    │   └── 3.md
    └── logs/
        ├── userspec/
        │   └── interview.yml         # Запись интервью
        ├── techspec/
        │   └── code-research.md      # Результат code-researcher
        ├── tasks/                    # Логи валидации задач
        ├── working/
        │   ├── task-1/               # JSON-отчёты ревьюеров
        │   ├── audit/                # Отчёты Audit Wave
        │   └── qa-report.json        # Pre-deploy QA
        ├── execution-plan.md         # План выполнения волн
        └── checkpoint.yml            # Состояние для восстановления сессии
```

Завершённые фичи: `work/completed/{feature}/`

---

## Структура глобальной папки `~/.claude/`

```
~/.claude/
├── agents/           # 21 агент
├── commands/         # 9 slash-команд
├── skills/           # 19 скиллов
├── shared/
│   ├── interview-templates/   # feature.yml, skill.yml
│   ├── work-templates/        # шаблоны документов
│   │   ├── user-spec.md.template
│   │   ├── tech-spec.md.template
│   │   ├── tasks/task.md.template
│   │   ├── decisions.md.template
│   │   ├── checkpoint.yml.template
│   │   └── execution-plan.md.template
│   ├── templates/new-project/ # шаблон нового проекта
│   └── scripts/               # init-feature-folder.sh
├── hooks/
│   └── post-compact-restore.sh
└── CLAUDE.md
```

---

## Все агенты (21 штука)

### Валидаторы спецификаций

| Агент | Когда вызывается | Что проверяет |
|-------|-----------------|--------------|
| `interview-completeness-checker` | После интервью в user-spec-planning | Пробелы в интервью, готовность к написанию спеки |
| `userspec-quality-validator` | После создания user-spec | Структура документа, тестируемость acceptance criteria, нет placeholder |
| `userspec-adequacy-validator` | Параллельно с quality | Осуществимость решения, масштаб (S/M/L), нет over/underengineering |
| `tech-spec-validator` | После создания tech-spec | Шаблон, frontmatter, качество задач, конфликты зависимостей между волнами |
| `skeptic` | Параллельно с tech-spec | Миражи — несуществующие файлы, функции, API в specs |
| `completeness-validator` | Параллельно с tech-spec | Двусторонняя трассировка: все требования user-spec есть в tech-spec и наоборот |
| `task-creator` | В decompose-tech-spec | Создаёт task-файлы из Implementation Tasks секции tech-spec |
| `task-validator` | После создания задач | Шаблон задачи, frontmatter, 11 обязательных секций, атомарность |
| `reality-checker` | Параллельно с task-validator | Существуют ли файлы/функции/APIs указанные в задачах |
| `skill-checker` | При создании новых скиллов | Соответствие стандартам skill-master |

### Ревьюеры кода

| Агент | Что проверяет | Модель |
|-------|--------------|--------|
| `code-reviewer` | 11 измерений: архитектура, читаемость, error handling, типы, тесты, зависимости, безопасность, производительность, cross-file consistency, resource management | sonnet |
| `test-reviewer` | Качество тестов: litmus test на каждый тест, 8 anti-patterns, конкретные исправления | sonnet |
| `security-auditor` | OWASP Top 10: инъекции, auth, криптография, hardcoded secrets = всегда critical | sonnet |
| `prompt-reviewer` | LLM-промпты по принципам prompt-master | sonnet |
| `deploy-reviewer` | CI/CD workflows, secrets management, platform config | sonnet |
| `infrastructure-reviewer` | Структура папок, Docker, pre-commit hooks, .gitignore безопасность | sonnet |
| `documentation-reviewer` | Project Knowledge: полнота, нет bloat, нет code blocks, актуальность | sonnet |

### Исследователи

| Агент | Что делает |
|-------|-----------|
| `code-researcher` | Исследует кодовую базу перед tech-spec: entry points, data models, похожие реализации, тесты, risks. Пишет `code-research.md` |

### QA

| Агент | Когда | Что делает |
|-------|-------|-----------|
| `pre-deploy-qa` | Final Wave, до деплоя | Запускает тесты, проверяет каждый acceptance criteria. Пишет JSON в `logs/working/qa-report.json` |
| `post-deploy-qa` | Final Wave, после деплоя | Верификация на живом окружении: Playwright, curl, Telegram MCP, bash |

### Формат ответа агентов

Все агенты возвращают JSON:
```json
{
  "status": "approved | changes_required | passed | failed",
  "findings": [
    {
      "severity": "critical | major | minor",
      "location": "файл:строка",
      "issue": "описание",
      "remediation": "как исправить"
    }
  ],
  "summary": "краткий итог"
}
```

`approved/passed` = 0 критических. Низкий порог — false positive дешевле пропущенной проблемы.

---

## Все скиллы (19 штук)

### Планирование

**`project-planning`** — `/init-project-knowledge`
Интервью о новом проекте → заполняет все 5 файлов Project Knowledge. Работает поэтапно: по одному вопросу, подтверждает каждые 3-5 вопросов.

**`user-spec-planning`** — `/new-user-spec`
9 фаз: читает project knowledge → сканирует код → 3 цикла интервью → создаёт user-spec → 2 параллельных валидатора → одобрение. Стиль: вовлечённый соучастник, предлагает решения на основе архитектуры.

**`tech-spec-planning`** — `/new-tech-spec`
6 фаз: контекст → code-researcher → уточняющие вопросы → пишет tech-spec (Edit по секциям) → 5 параллельных валидаторов → одобрение. Задачи в tech-spec = только ЧТО и ЗАЧЕМ, не КАК.

**`task-decomposition`** — `/decompose-tech-spec`
3 фазы: параллельное создание задач → валидация (task-validator + reality-checker) → перекрёстная проверка → одобрение.

---

### Разработка

**`code-writing`** — `/do-task`, `/write-code`
3 фазы:
1. Подготовка: читает требования, project.md, architecture.md, patterns.md
2. TDD цикл: пишет тесты → реализует → тесты проходят
3. Пост-работа: lint, тесты, smoke, параллельные ревью. JSON-отчёты в `logs/working/task-N/`. Максимум 3 раунда.

**`feature-execution`** — `/do-feature`
Тимлид-диспетчер. Фазы: инициализация → волны → переходы → ревью пользователем. Checkpoint после каждой волны. Только оркестрация, никакого кода.

**`prompt-master`**
Написание LLM-промптов. Принципы: модель уже умная (не объясняй очевидное), ясность > хитрость, мотивация > акцент, позитивные инструкции, примеры > правила, сжимай. Структура через XML теги.

---

### Качество

**`code-reviewing`**
11 измерений: архитектурные паттерны, separation of concerns, читаемость, error handling (таблица severity), типобезопасность, покрытие тестами, зависимости, безопасность, производительность, cross-file consistency, resource management (синглтоны, не создавать per-request).

**`test-master`**
Тестовая пирамида: Smoke → Unit → Integration → E2E. Таблицы решений когда что использовать. 10 правил. Litmus test: "если убрать бизнес-логику, тест всё равно проходит?" → плохой тест. 3+ мока = не тот тип теста.

**`security-auditor`**
OWASP Top 10. Методология: трассируем entry points → data flow → DB queries → input handling → зависимости. Severity: Critical/High/Medium/Low. Hardcoded secrets = автоматически Critical.

**`pre-deploy-qa`**
Находит тест-раннер (package.json / pyproject.toml / Makefile), запускает все тесты, проверяет каждый acceptance criteria. JSON-отчёт.

**`post-deploy-qa`**
Верификация на живом окружении. Два трека: Agent Verification Plan из specs + acceptance criteria. Инструменты: Telegram MCP, Playwright, curl, bash.

---

### Инфраструктура

**`infrastructure-setup`**
7 фаз: framework init → структура папок → Docker (multi-stage, non-root, alpine) → .gitignore (секреты) → pre-commit hooks (gitleaks <10s) → тестирование (1-2 smoke теста) → документация. Checkpoint: коммитим файл с тестовым секретом, gitleaks должен заблокировать.

**`deploy-pipeline`**
CI/CD для разных платформ: Vercel, Railway, Fly.io, AWS ECS, VPS, NPM, Chrome Web Store. GitHub Actions: skip-check → test → deploy. Secrets только через GitHub Actions secrets.

**`documentation-writing`**
Управление Project Knowledge. Принципы: одно место для каждого факта, без code blocks (только ссылки на файлы), patterns.md = только специфичное для проекта. Воркфловы: Audit (bloat, дубли, placeholder) → Edit → Consistency → Status.

---

### Мета

**`methodology`**
Описание всей системы. Читать когда нужно объяснить как работает pipeline целиком.

**`skill-master`**
Создание и обновление скиллов. Два типа: процедурный (строгая последовательность) и информационный (независимые секции). Структура: SKILL.md + references/ + scripts/. Без слов CRITICAL/MANDATORY (антипаттерн).

**`skill-tester`**
Полный цикл тестирования скиллов: 2 раннера со скиллом + 1 без. Оценка assertions: бинарные, наблюдаемые. Метрики: точность триггеров ≥85%, false negative ≤20%.

---

## Команды

| Команда | Скилл | Что делает |
|---------|-------|-----------|
| `/init-project` | — | Создаёт структуру из шаблона, git init, gh repo create (private), ветки main+dev |
| `/init-project-knowledge` | `project-planning` | Интервью → заполняет все PK файлы + бэклог |
| `/new-user-spec` | `user-spec-planning` | Интервью → user-spec.md с валидацией |
| `/new-tech-spec` | `tech-spec-planning` | Code research → tech-spec.md с 5 валидаторами |
| `/decompose-tech-spec` | `task-decomposition` | Tech-spec → task файлы с 2 валидаторами |
| `/do-task` | из frontmatter задачи | Одна задача: TDD + ревью |
| `/do-feature` | `feature-execution` | Вся фича командой агентов по волнам |
| `/write-code` | `code-writing` | Быстрый код без спеки |
| `/done` | `documentation-writing` | Обновляет PK, архивирует фичу |

---

## Коммит-стратегия

| Момент | Кто коммитит | Сообщение |
|--------|-------------|-----------|
| После создания draft spec/tasks | Главный агент | `docs: draft user-spec / tech-spec / tasks` |
| После раунда валидации | Главный агент | `docs: fix validation round N` |
| После одобрения | Главный агент | `docs: approve user-spec / tech-spec / tasks` |
| После реализации (тесты прошли) | Teammate | `feat\|fix: task N — краткое описание` |
| После раунда ревью | Teammate | `fix: address review round M for task N` |
| После ревью-отчётов | Teammate | `chore: review reports for task N` |
| После волны | Тимлид | `chore: complete wave N — update task statuses and decisions` |
| Эскалация | Тимлид | `chore: escalate task N — unresolved after 3 fix rounds` |
| Финализация | Главный агент | обычный коммит с обновлением PK |

---

## Checkpoint — восстановление после компакции контекста

Если Claude Code сжал контекст во время выполнения `/do-feature`:

1. Хук `post-compact-restore.sh` срабатывает при SessionStart
2. Находит `checkpoint.yml` в активных фичах (не в completed/)
3. Проверяет что текущая сессия — тимлид
4. Выводит инструкции для возобновления

Тимлид при старте читает checkpoint.yml:
- `last_completed_wave > 0` → возобновление
- Читает decisions.md для подтверждения что реально выполнено
- Задачи с записью в decisions.md = выполнены, пропустить
- Задачи без записи = выполнить заново
- Если команда жива (`~/.claude/teams/{team}/config.json`) — не пересоздавать

---

## Шаблоны документов

Все шаблоны в `~/.claude/shared/work-templates/`:

**user-spec.md.template** — frontmatter (created, status, type, size) + секции: Что делаем, Зачем, Как должно работать, Критерии приёмки, Ограничения, Риски, Технические решения, Тестирование, Как проверить (агент + пользователь)

**tech-spec.md.template** — frontmatter (created, status: draft→approved, branch, size: S|M|L) + секции: Solution, Architecture (components, data flow, shared resources), Decisions (с альтернативами), Data Models, Dependencies, Testing Strategy, Agent Verification Plan, Risks, User-Spec Deviations (с пометкой PENDING USER APPROVAL), Acceptance Criteria, Implementation Tasks (по волнам с Skill/Reviewers/Verify-smoke/Verify-user/Files)

**task.md.template** — YAML frontmatter (status, depends_on, wave, skills, reviewers, verify) + Description, What to do, TDD Anchor, Acceptance Criteria, Context Files, Verify-smoke, Verify-user, Reviewer Assignments, Post-completion

**decisions.md.template** — лог решений по задачам: Task N, Status, Commit, Agent, Summary, Deviations, Reviews (ссылки на JSON), Verification

**checkpoint.yml.template** — skill, feature, feature_dir, team_name, last_completed_wave, total_waves, tasks (статусы), next_wave

**execution-plan.md.template** — план по волнам: Wave 1 (независимые задачи), Wave 2 (зависит от Wave 1), проверки требующие участия пользователя

---

## Принципы качества тестов

Litmus test: "Если убрать бизнес-логику, тест всё равно пройдёт?" → плохой тест.

8 антипаттернов:
1. `empty_test` — expect(true).toBe(true)
2. `mock_only` — тест только мокает, не проверяет логику
3. `missing_coverage` — нет проверки требований
4. `pyramid_violation` — E2E там где нужен unit
5. `excessive_mocking` — 3+ мока = не тот тип теста
6. `anti_pattern` — тест реализации а не поведения
7. `wrong_test_type` — unit там где нужен integration
8. `redundant_testing` — то же что другой тест

Мокать: DB, внешние API, файловая система, время, random.
НЕ мокать: бизнес-логику, трансформации, вычисления.

---

## Требования к окружению

- Claude Code CLI
- Context7 MCP сервер — для актуальной документации библиотек
- gh CLI — для работы с GitHub
- gitleaks — для pre-commit хука на секреты
- Docker (опционально, для инфраструктурных проектов)

---

## Быстрый старт нового проекта

```
/init-project              # структура + git + GitHub
/init-project-knowledge    # интервью → заполняет PK файлы
/new-user-spec             # первая фича: интервью с тобой
/new-tech-spec             # архитектура
/decompose-tech-spec       # задачи
/do-feature                # поехали
/done                      # закрыли
```

---

*Источник: github.com/pavel-molyanov/molyanov-ai-dev | MIT License*
