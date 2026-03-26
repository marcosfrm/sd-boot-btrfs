Este script serve para criar snapshots do diretório raiz em Btrfs do meu Arch Linux, com a seguinte configuração de subvolumes:

```console
$ findmnt /dev/sda2 -o TARGET,SOURCE
TARGET                  SOURCE
/                       /dev/sda2[/@]
/snapshots              /dev/sda2[/@snapshots]
/swap                   /dev/sda2[/@swap]
/var/lib/libvirt/images /dev/sda2[/@var-lib-libvirt-images]
/home                   /dev/sda2[/@home]
```

A partição EFI está montada em `/boot`:

```console
$ findmnt /dev/sda1 -o TARGET,SOURCE
TARGET SOURCE
/boot  /dev/sda1
```

O script, instalado em `/usr/local/bin/sd-boot-btrfs.py`, roda semanalmente através do systemd e requer `python-dasbus`.

```console
$ systemctl cat sabugo-btrfs.timer
# /etc/systemd/system/sabugo-btrfs.timer
[Unit]
Description=Snapshot semanal do subvolume raiz

[Timer]
OnCalendar=weekly
RandomizedDelaySec=2h
Persistent=true

[Install]
WantedBy=timers.target
```

```console
$ systemctl cat sabugo-btrfs.service
# /etc/systemd/system/sabugo-btrfs.service
[Unit]
Description=Cria snapshot do subvolume raiz
#ConditionACPower=true

[Service]
Type=oneshot
ExecStart=sd-boot-btrfs.py
```