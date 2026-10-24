# Context window: usage, cost and compaction

Цель: показать в Logos заполнение контекстного окна и стоимость сессии так же
понятно, как в opencode, а затем использовать это для своевременного сжатия
истории.

## Что делают другие

- **opencode.** В заголовке сессии показывает токены последнего ответа
  (`input + output + reasoning + cache.read + cache.write`), процент от
  `model.limit.context` и суммарную стоимость сессии. Во вкладке Context есть
  разбивка (system/user/assistant/tool/other) и статистика сессии. Сжатие
  (`session/compaction.ts`) запускается при переполнении; старые tool-выводы
  сначала подрезаются (`PRUNE_MINIMUM = 20k`, `PRUNE_PROTECT = 40k`,
  `TOOL_OUTPUT_MAX_CHARS = 2k`), а последние сообщения сохраняются целиком
  (бюджет `min(15k, max(2k, 25% usable))`).
- **Claude Code.** Автоматически сжимает историю при приближении к лимиту,
  сохраняет системный промпт, правила проекта и часть недавних файлов;
  поддерживает ручной `/compact` и настраиваемый порог.
- **Cline / OpenHands.** Тоже используют порог по токенам/событиям и
  LLM-конспект старой части истории; пользователь может запустить сжатие
  вручную.
- **Общий вывод.** Надёжнее не «сжимать при 100%», а показывать заполнение
  заранее, сжимать около 60-90% и всегда сохранять недавний хвост дословно.

## Текущее состояние Logos

- `context/tokens.py` умеет грубо оценивать токены и знает `context_window`
  из `models.json` (fallback 128k).
- `context/handoff.py` уже обновляет rolling-конспект при достижении
  `context.summary_threshold_ratio` (по умолчанию 60%).
- Провайдеры теперь собирают нормализованный `usage` (OpenAI Responses/CUA,
  OpenRouter, Anthropic, Google, xAI, HuggingFace), он пишется в JSONL отдельным
  событием. Если провайдер usage не вернул, остаётся оценка по тексту.
- Стоимость берётся из ответа провайдера (OpenRouter) либо считается по
  необязательным ценам `*_cost_per_mtok` в `models.json`; без них честно
  показывается «цена неизвестна».
- В диалоге **Context** блок «Context window» показывает фактическое заполнение
  (последний ответ) либо оценку, лимит, процент, порог, счётчики, разбивку
  usage, стоимость сессии/последнего запроса и предупреждение при 80%.
- Перед суммаризацией старые tool-выводы подрезаются, недавний хвост
  сохраняется, а ручное сжатие доступно кнопкой **Compact now**.
  Повторные неудачи LLM-сжатия ограничены backoff-ом.

## Этапы

### Этап 1 - оценочная статистика (сделано)

- `src/context/context_stats.py`: единый снимок по journal-событиям.
- `src/controller.py`: прогресс-бар и текстовые метрики в диалоге Context.
- Числа помечены как оценка; ничего не сохраняется и не влияет на решения
  агента.
- Проверка: `ruff`, `compileall`, smoke-тест модуля и диалога.

### Этап 2 - реальный `usage` от провайдеров (сделано; нужна live-проверка)

1. Введена нормализованная запись:
   ```json
   {"type":"usage","input":0,"output":0,"reasoning":0,"cache_read":0,
    "cache_write":0,"cost":null,"currency":"USD","provider":"...","model":"..."}
   ```
2. Usage собирается во всех провайдерах:
   - OpenAI Responses: `response.usage` / событие `response.completed`;
   - OpenRouter: `stream_options.include_usage`, `usage` в чанке и
     `/generation?id=` для `total_cost`;
   - Anthropic: `message_start` / `message_delta.usage` (включая CUA);
   - Google: `usage_metadata` и `usage` у Interactions API (CUA);
   - xAI и HuggingFace: OpenAI-совместимый `usage`.
3. `LLMWorker` шлёт `usage_received`, контроллер пишет `usage`-событие после
   ответа; CUA-воркер шлёт usage отдельным сигналом.
4. Для процента окна берётся **последний** ответ (как opencode), для суммарных
   токенов сессии - сумма по всем ответам (input/output/reasoning/cache).
5. Диалог показывает «actual», если usage есть, иначе «estimated»; стоимость
   показывается при наличии провайдерского `cost` или цен модели.

### Этап 3 - стоимость (сделано, данные цен опциональны)

1. `ModelMetadata.pricing()` читает из `models.json` поля
   `input_cost_per_mtok`, `output_cost_per_mtok`,
   `cache_read_cost_per_mtok`, `cache_write_cost_per_mtok`.
2. Провайдерский `cost` (OpenRouter) имеет приоритет; иначе стоимость считается
   после полного ответа по ценам модели и помечается `cost_source`.
3. Диалог показывает стоимость сессии и последнего запроса; если цен нет -
   пишет «цена неизвестна», не выдумывая число.
4. При фактическом заполнении окна ≥ 80% один раз на сессию выводится
   неблокирующее предупреждение.

### Этап 4 - сжатие в стиле opencode (сделано)

1. `handoff._select_event_lines()` подрезает старые большие `tool_result` /
   `job_result` до 2k символов с сохранением начала, конца и статуса.
2. Последние ~20k символов (до 12k на событие) сохраняются без pruning;
   общий prompt ограничен 40k символов.
3. `needs_refresh()` использует фактический usage последнего ответа, а при его
   отсутствии - оценку по journal-тексту.
4. В диалоге Context есть **Compact now**; после ошибки LLM-сжатия действует
   backoff 15–300 секунд, чтобы не зацикливать неудачные попытки.

## Файлы

- `src/context/context_stats.py` - статистика сессии (actual + estimate).
- `src/controller.py` - диалог Context, запись `usage`-событий.
- `src/context/tokens.py` - окно контекста и необязательные цены модели.
- `src/context/handoff.py` - порог, pruning и rolling summary.
- `src/llm/usage.py` - нормализация usage разных провайдеров.
- `src/llm/base_provider.py`, `src/llm/llm_worker.py`,
  `src/llm/providers/*.py` - сбор и передача usage.
- `src/handlers/vision_handler.py` - usage из CUA-воркера.
- `src/project_manager.py` - JSONL `usage`-event.
- `docs/context_handoff_plan.md` - исходный план handoff.

## Риски

- Оценка по символам может заметно расходиться с токенизатором, особенно на
  изображениях и reasoning.
- Провайдеры отдают usage по-разному; нужен безопасный fallback, чтобы
  отсутствие поля не ломало ответ.
- Цены меняются; без обновляемого источника стоимость быстро устареет.
