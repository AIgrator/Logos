# TODO — отложенные правки

Список вещей, которые договорились сделать позже (не сейчас). Каждый пункт —
что, где в коде и почему отложено.

---

## 1. Кнопка Stop в заголовке окна (compact/Vision) не должна сбрасывать режим в чат

**Статус:** исправлено в коде, ожидает проверки пользователем (Vision/compact-flow).
**Дата:** октябрь 2026.

> Реализация: `_handle_stop` (`src/controller.py`) теперь останавливает агент
> режима (Vision task либо замороженную Browser/Terminal-сессию) и **переиспользует
> `handle_cancel_request()`**, который оставляет выбранный режим
> (`_selected_mode_state()`) вместо `_activate_chat_mode()`. См. также коммит с
> фиксом `handle_cancel_request` (f4391f8).

**Что:** отдельная кнопка **Stop в заголовке окна** (компактное окно, которое
появляется во время визуальной/агентной работы) сейчас принудительно переключает
приложение в чат. Надо, как и у кнопки отмены в тулбаре, **сохранять выбранный
режим** (Browser / Terminal / Vision), а не падать в чат.

**Где:**
- `src/ui/manager.py`: `stop_clicked` (сигнал заголовка) → `_on_stop_clicked` →
  `Controller.stop_clicked`.
- `src/controller.py`: `self.ui.stop_clicked.connect(self._handle_stop)` (строка ~179).
- `src/controller.py`: `_handle_stop()` (строка ~1135) — в конце вызывает
  `self._activate_chat_mode()` («Chat mode enabled.»). Именно это и надо заменить.

**Как чинить (ориентир):** смотри уже исправленную кнопку отмены в тулбаре —
`handle_cancel_request()` (`src/controller.py:856`) и хелпер
`_selected_mode_state()` (`src/controller.py`), который маппит текущий UI-режим
(`ui._active_mode_key`) в `ControllerState.BROWSER/TERMINAL/VISION`, а для чата —
в `IDLE`. `_handle_stop` должен останавливать агент-хендлер и **возвращаться в
выбранный режим**, а не вызывать `_activate_chat_mode()`.

**Почему отложено:** Stop в заголовке связан с Vision-режимом и compact-окном;
нужно отдельно проверить взаимодействие со стримингом и состоянием Vision, чтобы
не сломать уже работающий сценарий.
