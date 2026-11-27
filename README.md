<div align="center">

# ⚡ Antigravity Patcher

**CLI-инструмент, расширение для IDE / Desktop 2.0 и оркестратор мультиагентного роя для Google Antigravity**

[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue?style=flat-square)](./LICENSE)
[![Author: EnergyStar](https://img.shields.io/badge/GitHub-EnergyStario-181717?style=flat-square&logo=github)](https://github.com/EnergyStario)
[![Telegram](https://img.shields.io/badge/Telegram-@energystar6-26A5E5?style=flat-square&logo=telegram)](https://t.me/energystar6)

</div>

---

## Что внутри

- **Antigravity Assistant (`IDE` и `Antigravity 2.0`):** строка состояния с живыми лимитами по корзинам моделей (`Claude / GPT`, `Hydrogen / 3.8 Flash`, `Gemini 4 Carbon / Argon`), временем сброса и переключением аккаунтов без лишних действий.
- **Умная ротация аккаунтов (`429 / 0%`):** каскадная защита умных моделей (другой аккаунт → независимая корзина Tier-1 → резервный `Flash`) с раздельными слотами под `IDE`, `Antigravity 2.0` и `agy CLI`.
- **Multi-Agent Swarm (`ag-swarm`):** параллельная работа нескольких агентов в изолированных ветках `git worktree` (правило `1 агент = свои файлы`), чекпоинты `state.json`, AST-выборка тестов перед мерджем и безопасная интеграция в `main`.
- **Error Guardian & Сеть (`NRPT DNS` / `Hosts` / `TLS`):** автоматическая диагностика обрывов прокси, сетевых сбоев `ECONNRESET`/`EOF` и восстановление истории диалогов.
- **CLI без лишнего веса:** быстрый консольный запуск и авто-перепатчинг после обновлений целевых приложений.

---

## Скачать готовые сборки ([Releases](https://github.com/EnergyStario/antigravity-patcher/releases/latest))

Готовые бинарные файлы (без установки Python) публикуются в разделе **[Releases](https://github.com/EnergyStario/antigravity-patcher/releases/latest)**:

| Платформа | Файл в релизе |
| :--- | :--- |
| **Windows (x64)** | `Antigravity_Patcher_Windows_x64.exe` |
| **macOS (Apple Silicon)** | `Antigravity_Patcher_macOS_arm64.tar.gz` |
| **Linux (x64)** | `Antigravity_Patcher_Linux_x64.tar.gz` |
| **IDE-расширение (VSIX)** | `ag-toolkit-1.2.1.vsix` |

---

## Быстрый старт (из исходников)

> Требуется **Python 3.10+** и **Git** (для `ag-swarm`). На Windows для настройки системного DNS и защиты от автообновлений запускайте терминал от имени администратора.

```bash
git clone https://github.com/EnergyStario/antigravity-patcher.git
cd antigravity-patcher
python source/main.py
```

Доступные флаги CLI:

| Флаг | Назначение |
| :--- | :--- |
| `python source/main.py` | Интерактивное консольное меню (настройка IDE, 2.0, CLI, сети и истории) |
| `python source/main.py --status` | Быстрая диагностика установленных компонентов |
| `python source/main.py --auto-patch` | Тихое обновление конфигурации без меню |
| `python source/main.py --autopilot-heal` | Проверка и авто-лечение сети/прокси по логам |

Установка расширения вручную (Windows / macOS / Linux):

```powershell
# Windows
.\install.ps1
```

```bash
# macOS / Linux
bash ./install.sh
```

---

## Команда агентов (`ag-swarm`)

Запуск прямо из чата Antigravity IDE, 2.0 или `agy` CLI:

```text
/swarm Реализуй новый эндпоинт в api.py, покрой тестами в test_api.py и обнови README
```

1. **Изоляция `git worktree`:** каждый воркер работает в своей ветке и папке — файлы не перезаписываются.
2. **Проверка до `main`:** сначала запускаются затронутые тесты (AST-анализ импортов), затем ветки собираются во временный worktree `_integration` и только после полного прохождения тестов сдвигается `main`.
3. **Откат и возобновление:** прогресс сохраняется в `~/.antigravity_tools/swarm/<task_id>/state.json` — любую прерванную задачу можно продолжить через `python -m swarm.cli resume --task-id <task_id>`.

---

## Структура репозитория

```text
source/       # Ядро CLI, диагностика, сетевые правила (NRPT/Hosts) и Swarm Engine
extensions/   # Плагин для IDE, встроенный UI для Antigravity 2.0 и движок квот
tests/        # Юнит- и интеграционные тесты (pytest + node)
install.*     # Скрипты установки расширения
```

---

## Лицензия и контакты

- **Автор:** [EnergyStar (`EnergyStario`)](https://github.com/EnergyStario) · Telegram: [@energystar6](https://t.me/energystar6)
- **Лицензия:** [GPL-3.0](./LICENSE)
