# Результат проверки

Проверено непосредственно на локальной Ubuntu 22.04.5 LTS x86_64 с ядром 6.8.0-138-generic и аппаратным KVM (`kvm_amd`). `/dev/kvm` доступен текущему пользователю. Каждая тестовая ВМ использовала 1 vCPU, 768 МиБ RAM, виртуальный диск 4 ГиБ и QEMU user-mode NAT. Две ВМ запускались последовательно, без GUI.

Образ: официальный Ubuntu 22.04 Minimal cloud image, скачанный с `cloud-images.ubuntu.com/minimal/releases/jammy/release/ubuntu-22.04-minimal-cloudimg-amd64.img`.

SHA256 образа совпал с опубликованным `SHA256SUMS`:

```text
6f815a0e248991b9a57be75cef992d611fa15aa4eef3461affc1389e184e89c5
```

Результат `check.py` (код завершения 0):

```text
PASS: image registration
PASS: headless QEMU/KVM VM started
PASS: SSH key, hostname and completed cloud-init inside guest
PASS: CPU/RAM quota rejection
PASS: service restart preserves guest and image registry
PASS: stop and start
PASS: delete and cleanup
PASS: custom user_data applied in real guest
All managed VMs, disks, seeds and image registrations removed. Base images kept.
PASS: offline cleanup of running VM; all checks passed
```

Проверка выполняла команды внутри настоящих гостей по SSH; завершение cloud-init подтверждено `cloud-init status --wait` и созданными внутри гостя файлами. После проверки тестовые ВМ остановлены и их временные диски, ISO, ключи и логи удалены. Общедоступный репозиторий содержит только исходники и документацию.
