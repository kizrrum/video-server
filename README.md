# 🎬 Video Server

Простой видеосервер для локальной сети.  
Показывает видео в браузере, перекодирует на лету неподдерживаемые форматы, создаёт M3U-плейлисты для VLC.

---

## 📦 Требования

- **Python 3.7+**
- **FFmpeg** (должен быть в PATH)

### Установка FFmpeg

| Платформа | Команда |
|-----------|---------|
| **Linux** (Debian/Ubuntu) | `sudo apt install ffmpeg` |
| **macOS** | `brew install ffmpeg` |
| **Windows** | Скачайте с [ffmpeg.org](https://ffmpeg.org/download.html) и добавьте `bin` в PATH |

---

## 🚀 Быстрый старт

Клонируйте репозиторий:

```bash
git clone https://github.com/kizrrum/video-server.git
cd video-server
```

### Запуск

**Linux / macOS:**

```bash
export BASE_DIR=/путь/к/видео
python3 video_web_server.py
```

**Windows (CMD):**

```cmd
set BASE_DIR=C:\путь\к\видео
python video_web_server.py
```

**Windows (PowerShell):**

```powershell
$env:BASE_DIR = "C:\путь\к\видео"
python video_web_server.py
```

После запуска откройте браузер и перейдите по адресу:

```
http://localhost:8001
```

---

## ⚙️ Основные переменные окружения

| Переменная | Значение по умолчанию | Описание |
|-----------|----------------------|----------|
| `BASE_DIR` | `/media/320-sata/tor` | Корневая папка с видео |
| `DB_PATH` | `/var/cache/video_server/metadata.db` | Путь к БД с кэшем |
| `PORT` | `8001` | Порт сервера |
| `AUTH_USER` | (пусто) | Логин для доступа |
| `AUTH_PASS` | (пусто) | Пароль для доступа |
| `URL_PREFIX` | (пусто) | Префикс URL (например, `/video`) |
| `FFMPEG_PRESET` | `ultrafast` | Скорость кодирования |
| `FFMPEG_CRF` | `28` | Качество (меньше = лучше) |
| `FFMPEG_THREADS` | `1` | Потоков x264 |
| `FFMPEG_NICE` | `10` | Приоритет процесса (Linux) |
| `MAX_TRANSCODE_JOBS` | `1` | Одновременных транскодов |
| `TRANSCODE_QUEUE_SEC` | `300` | Таймаут ожидания транскода |
| `SCAN_WORKERS` | `2` | Потоков при сканировании |

Полный список — в коде, в разделе `# КОНФИГУРАЦИЯ`.

---

## 🧩 Возможности

- 📁 **Навигация** по папкам с сортировкой и поиском (включая подпапки)
- 🎞 **Воспроизведение** в браузере через HTML5-видео
- ⚡ **Перекодирование** на лету (AVI, AC3, DTS → MP4/H.264/AAC)
- 📁 **M3U-плейлисты** для VLC и других плееров
- 🔍 Мгновенный поиск по индексу SQLite (не по файловой системе)
- 🔐 **Аутентификация** (опционально)
- 🧵 **Многопоточное сканирование** с прогрессом
- 📊 **Статус сканирования** через API `/status`
- 🗂️ **Фильтрация пустых папок** — показываются только папки с видео
- 💻 **Кроссплатформенность** — работает на Windows, Linux, macOS

---

## 🧪 Проверка работоспособности

```bash
curl http://localhost:8001/health
```

Ожидаемый ответ:

```json
{"status":"ok","base_dir":"/путь/к/видео"}
```

---

## 🐧 Запуск как сервис (Linux systemd)

Создайте файл `/etc/systemd/system/video-server.service`:

```ini
[Unit]
Description=Python Video Server
After=network.target

[Service]
WorkingDirectory=/путь/к/репозиторию
Environment="BASE_DIR=/путь/к/видео"
Environment="DB_PATH=/var/cache/video_server/metadata.db"
Environment="AUTH_USER=admin"
Environment="AUTH_PASS=your_password"
Environment="PORT=8001"
Environment="FFMPEG_PRESET=ultrafast"
Environment="FFMPEG_CRF=28"
Environment="MAX_TRANSCODE_JOBS=1"
ExecStart=/usr/bin/python3 /путь/к/репозиторию/video_web_server.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Затем:

```bash
sudo systemctl daemon-reload
sudo systemctl enable video-server
sudo systemctl start video-server
```

---

## 📄 Лицензия

MIT
