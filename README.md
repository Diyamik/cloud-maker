# Cloud Maker

Небольшой HTTP API на Python для управления настоящими виртуальными машинами QEMU/KVM на Linux. Управление QEMU идёт через QMP Unix socket. Создание и загрузка выполняются без графического интерфейса.

## Реализованные пункты задания

| Пункт CloudMaker.pdf | Реализация | Баллы по условию |
|---|---|---:|
| Базовый | Запуск Ubuntu в QEMU/KVM без GUI, инструкция проверки | 4 |
| 1 | HTTP API: создание, просмотр, остановка, повторный запуск, удаление ВМ | 2 |
| 2 | NoCloud ISO: hostname, SSH-ключи, произвольный `user_data` | 2 |
| 6 | После перезапуска API обнаруживаются запущенные ВМ и восстанавливается управление через QMP | 2 |
| 7 | Реестр образов: название, абсолютный путь, описание | 2 |
| 9 | Квоты CPU/RAM с сериализацией операций создания | 2 |
| 11 | Один скрипт проверяет все перечисленные возможности на настоящих ВМ | 2 |
| 12 | Скрипт очистки останавливает ВМ и удаляет тестовые диски и cloud-init ISO | 1 |

Сумма реализованных пунктов: **17 баллов по условию**, окончательную оценку определяет преподаватель. Пункты 3–5, 8, 10 не заявляются. Контейнеризация не используется; восстановление реализовано для перезапуска сервиса (разрешённый условием вариант пункта 6).

## Требования

Ubuntu 22.04/24.04 x86_64, Python 3.10+, QEMU с KVM, доступ к `/dev/kvm`, OpenSSH client, curl. Интернет нужен при подготовке образа и зависимости. В штатном запуске сервису не нужен root. Минимум для проверки: 2 свободных CPU, 1.5 ГиБ RAM, около 2 ГиБ свободного диска. Проверка создаёт ВМ последовательно.

Если пакеты отсутствуют:

```bash
sudo apt update
sudo apt install -y qemu-system-x86 qemu-utils python3-venv openssh-client curl
```

Проверка аппаратной виртуализации:

```bash
grep -E 'vmx|svm' /proc/cpuinfo | head -1
ls -l /dev/kvm
test -r /dev/kvm && test -w /dev/kvm && echo 'KVM доступен'
```

При отсутствии `/dev/kvm` включите аппаратную виртуализацию в BIOS/UEFI и загрузите соответствующий модуль (`sudo modprobe kvm_amd` для AMD либо `sudo modprobe kvm_intel` для Intel). Если устройство есть, но доступа нет: `sudo usermod -aG kvm "$USER"`, затем полностью выйдите из сеанса и войдите снова. Сам API запускать через sudo не нужно.

## Подготовка

Все команды ниже выполняются в каталоге репозитория.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p state/images
curl -fL --retry 2 \
  https://cloud-images.ubuntu.com/minimal/releases/jammy/release/ubuntu-22.04-minimal-cloudimg-amd64.img \
  -o state/images/ubuntu-22.04-minimal-cloudimg-amd64.img
curl -fL \
  https://cloud-images.ubuntu.com/minimal/releases/jammy/release/SHA256SUMS \
  -o state/images/SHA256SUMS
(cd state/images && grep ' ubuntu-22.04-minimal-cloudimg-amd64.img$\|\*ubuntu-22.04-minimal-cloudimg-amd64.img$' SHA256SUMS | sha256sum -c -)
```

Образ в каталоге `release` обновляется. Файл контрольных сумм проверяет целостность загрузки через HTTPS; это не отдельная проверка GPG-подписи издателя. Скачанный образ должен оставаться неизменным и доступным: диски ВМ используют его как read-only backing file.

## Полная проверка одной командой

```bash
.venv/bin/python check.py --image state/images/ubuntu-22.04-minimal-cloudimg-amd64.img
```

Скрипт запускает отдельный API на свободном локальном порту, генерирует временную SSH-пару, регистрирует образ, создаёт ВМ с KVM, ждёт загрузки и завершения cloud-init, проверяет SSH, hostname и файл внутри гостя. Затем проверяет отклонение запроса сверх квоты, перезапуск сервиса с сохранением работающей ВМ, остановку/повторный запуск/удаление. На второй ВМ проверяет пользовательский `user_data` и отдельный скрипт очистки при остановленном API.

Ожидается последовательность `PASS: ...`, заканчивающаяся `PASS: offline cleanup of running VM; all checks passed`, и код завершения 0. Обычно занимает 1–3 минуты; на каждую загрузку установлен таймаут 240 секунд. При ошибке выводятся журнал API и хвост serial console. Тестовые диски, ключи и ВМ очищаются автоматически; базовый образ сохраняется.

## Ручной запуск

Для QMP нужен короткий путь к состоянию (до 70 символов). Все команды к сервису и очистке должны использовать **один и тот же** `CLOUDMAKER_STATE`:

```bash
export CLOUDMAKER_STATE="$PWD/state"
export CLOUDMAKER_MAX_CPU=4
export CLOUDMAKER_MAX_RAM_MB=4096
.venv/bin/python cloudmaker.py --port 8080
```

Если путь репозитория слишком длинный, используйте отдельный короткий каталог, например `export CLOUDMAKER_STATE=/tmp/cloudmaker-$USER`. После выхода из первого терминала повторите эту переменную во втором. Реестр и metadata сохраняются на диске; не удаляйте каталог, пока существуют ВМ.

Во втором терминале:

```bash
curl -fsS http://127.0.0.1:8080/health
.venv/bin/python - <<'PY'
import json, pathlib, urllib.request
body = json.dumps({
    'name': 'ubuntu',
    'path': str(pathlib.Path('state/images/ubuntu-22.04-minimal-cloudimg-amd64.img').resolve()),
    'description': 'Ubuntu 22.04 Minimal cloud image'
}).encode()
req = urllib.request.Request('http://127.0.0.1:8080/images', data=body,
                             headers={'Content-Type': 'application/json'})
print(urllib.request.urlopen(req).read().decode())
PY
```

Создание с существующим публичным SSH-ключом:

```bash
.venv/bin/python - <<'PY'
import json, pathlib, urllib.request
key = pathlib.Path.home() / '.ssh/id_ed25519.pub'
body = json.dumps({'image': 'ubuntu', 'hostname': 'demo', 'cpus': 1,
                   'memory_mb': 768, 'disk_gb': 4,
                   'ssh_keys': [key.read_text().strip()]}).encode()
req = urllib.request.Request('http://127.0.0.1:8080/vms', data=body,
                             headers={'Content-Type': 'application/json'})
print(urllib.request.urlopen(req).read().decode())
PY
```

Если ключа нет, создайте его: `ssh-keygen -t ed25519` (не перезаписывайте существующий ключ). Ответ содержит `id`, `status: "running"` и `ssh_port`. `running` означает работающий QEMU; гостю ещё нужно время на загрузку. После загрузки: `ssh -p <ssh_port> ubuntu@127.0.0.1`, затем `sudo cloud-init status --wait`.

```bash
curl -fsS http://127.0.0.1:8080/vms
curl -fsS http://127.0.0.1:8080/vms/<id>
curl -fsS -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8080/vms/<id>/stop
curl -fsS -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8080/vms/<id>/start
curl -fsS -X DELETE http://127.0.0.1:8080/vms/<id>
```

Замените `<id>` и `<ssh_port>` значениями ответа. Для пользовательского cloud-init передайте `user_data` как JSON-строку, начинающуюся с `#cloud-config\n`. В этом режиме переданный cloud-config полностью заменяет конфигурацию по умолчанию: пользователей и SSH-ключи надо описать в нём самостоятельно. `hostname` также передаётся в NoCloud `meta-data`.

## Очистка

Остановите API через Ctrl+C и выполните с тем же `CLOUDMAKER_STATE`:

```bash
.venv/bin/python cleanup.py
```

Скрипт завершает только ВМ из своего каталога состояния через их QMP sockets, удаляет их диски, ISO, логи и реестр образов. Сами базовые образы сохраняются. Каталог состояния и lock-файл остаются для безопасной синхронизации. Если API работает, очистка откажется выполняться. `check.py` использует собственный временный каталог и не затрагивает ручные ВМ.

## Устройство и ограничения

* `cloudmaker.py` — HTTP API, JSON metadata с атомарной записью, NoCloud ISO через pycdlib, QEMU и QMP. ВМ запускаются с `-enable-kvm -cpu host -display none -daemonize`.
* `check.py` — независимые проверки через HTTP и SSH; `cleanup.py` — отдельная офлайн-очистка.
* Сервис слушает только `127.0.0.1`, без аутентификации. Это локальный учебный инструмент для доверенного пользователя. Не публикуйте порт в интернет: API может читать локальные образы и запускать ВМ от своего пользователя. POST требует JSON, браузерные запросы с Origin отклоняются.
* Сеть — QEMU user-mode NAT и SSH port forwarding на loopback, без bridge. Системная сеть не изменяется.
* Остановка `/stop` — принудительный QMP `quit` (как выключение питания), а не graceful shutdown ОС; заранее сохраните данные. Удаление сначала останавливает ВМ. Для штатного завершения ОС используйте `sudo poweroff` внутри гостя.
* Квоты считают все созданные ВМ, включая остановленные. Это резервирование vCPU/RAM, не измерение фактической нагрузки. Для освобождения квоты удалите ВМ. На диск отдельной общей квоты нет; размер каждого диска ограничен 100 ГиБ.
* Восстановление после перезапуска API сохраняет работающие QEMU, hostname, SSH port и диски. После перезагрузки хоста ВМ будут показаны как stopped; автоматической загрузки ВМ нет. QMP sockets и metadata должны находиться в приватном каталоге состояния, одна служба на каталог.
* `user_data` применяется при первом запуске экземпляра, повторный start не создаёт новый instance-id.
* Образы должны быть самостоятельными qcow2 без цепочки backing files. Нельзя заменять/удалять образ, используемый существующей ВМ. Диск ВМ не может быть меньше виртуального размера образа.
* Журнал консоли: `$CLOUDMAKER_STATE/<id>/serial.log`. Ограничение пути QMP связано с длиной Unix socket в Linux.

## Проверенный запуск

См. [VALIDATION.md](VALIDATION.md) с результатом реального прогона на Ubuntu/KVM. Артефакты ВМ и приватные ключи в репозиторий не входят.
