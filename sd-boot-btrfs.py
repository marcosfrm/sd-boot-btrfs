#!/usr/bin/env python3

import btrfsutil, os, psutil, re, signal, shutil, sys
from datetime import datetime
from dasbus.connection import SystemMessageBus
from dasbus.unix import GLibClientUnix
from dasbus.error import DBusError
from contextlib import contextmanager

# CONFIGURAÇÃO
# =============================================================================
ORIGEM, DESTINO, MANTER = '/', '/snapshots', 4
CONFIG_ORIGEM = '/boot/loader/entries/arch-default.conf'
GER_PKG = ['/usr/bin/yay', '/usr/bin/pacman']
ESP_MNT, ESP_BKP = '/boot', '/boot-backup'

# AUXILIARES
# =============================================================================
@contextmanager
def inibe_desligamento():
    fd = None
    try:
        bus = SystemMessageBus()
        proxy = bus.get_proxy("org.freedesktop.login1", "/org/freedesktop/login1", client=GLibClientUnix)
        fd = proxy.Inhibit("sleep:shutdown:idle", os.path.basename(sys.argv[0]), f"Snapshot de {ORIGEM}", "block")
        yield
    finally:
        if fd is not None:
            os.close(fd)

def esperar_gerenciador_pacotes():
    # requer root para processos de usuários diferentes
    procs = [p for p in psutil.process_iter(['exe', 'name']) if p.info['exe'] in GER_PKG]
    if procs:
        detalhes = ", ".join(f"{p.pid}:{p.info['name']}" for p in procs)
        print(f"Aguardando {len(procs)} processo(s) [{detalhes}]...", file=sys.stderr)
        psutil.wait_procs(procs)

def backup_esp():
    if not (os.path.ismount(ESP_MNT) and os.access(ESP_MNT, os.R_OK | os.X_OK)):
        raise RuntimeError(f"{ESP_MNT} não está montado ou está inacessível.")

    if os.path.exists(ESP_BKP):
        if os.path.ismount(ESP_BKP):
            raise RuntimeError(f"{ESP_BKP} é um ponto de montagem!")
        shutil.rmtree(ESP_BKP)

    shutil.copytree(ESP_MNT, ESP_BKP)
    os.sync()

def criar_entrada_boot(snap_nome, dir_conf):
    snap_entrada = os.path.join(dir_conf, f"{snap_nome}.conf")
    # o ponto de montagem *atual* do destino difere do caminho do subvolume dentro do
    # sistema de arquivos em relação ao subvolume top-level
    subvol_path = os.path.join(btrfsutil.subvolume_path(DESTINO), snap_nome)

    with open(CONFIG_ORIGEM, 'r') as f:
        linhas = f.readlines()

    # entradas ficam no fim do menu
    nova_conf = ["sort-key zz-snapshot\n"]
    for l in linhas:
        l = l.strip()

        if l.startswith('sort-key'):
            continue

        if l.startswith('title'):
            nova_conf.append(f"{l} (snapshot {snap_nome})\n")
        elif l.startswith('options'):
            novo_subv = f"subvol={subvol_path}"
            nova_conf.append(f"{re.sub(r'subvol=[^\s]+', novo_subv, l)}\n")
        else:
            nova_conf.append(f"{l}\n")

    with open(snap_entrada, 'w') as f:
        f.writelines(nova_conf)
        f.flush()
        os.fsync(f.fileno())

def rotacionar_snapshots(snap_pref, dir_conf):
    with btrfsutil.SubvolumeIterator(DESTINO, info=True) as it:
        snaps = sorted([x for x in it if x[0].startswith(snap_pref)], key=lambda x: x[1].otime)

    for path, _ in snaps[:-MANTER]:
        snap_remove = os.path.join(DESTINO, path)
        btrfsutil.delete_subvolume(snap_remove)

        conf_remove = os.path.join(dir_conf, f"{path}.conf")
        if os.path.exists(conf_remove):
            os.remove(conf_remove)

            d_fd = os.open(dir_conf, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(d_fd)
            finally:
                os.close(d_fd)

# LÓGICA PRINCIPAL
# =============================================================================
def main():
    if os.geteuid() != 0:
        raise SystemExit("Este script precisa de privilégio de root.")

    # ignoramos SIGTERM
    # systemd enviará SIGKILL depois de TimeoutStopSec nos casos patológicos
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

    try:
        with inibe_desligamento():
            esperar_gerenciador_pacotes()
            backup_esp()

            snap_base = ORIGEM.strip('/').replace('/', '-')
            snap_pref = f"@{snap_base}_" if snap_base else "@_"
            snap_nome = f"{snap_pref}{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            snap_path = os.path.join(DESTINO, snap_nome)
            dir_conf = os.path.dirname(CONFIG_ORIGEM)

            btrfsutil.create_snapshot(ORIGEM, snap_path)

            try:
                criar_entrada_boot(snap_nome, dir_conf)
                status = 0
            except Exception as e:
                print(f"Erro ao criar entrada de inicialização: {e}. Desfazendo snapshot {snap_nome}.", file=sys.stderr)
                btrfsutil.delete_subvolume(snap_path)
                status = 1

            rotacionar_snapshots(snap_pref, dir_conf)
            return status

    except Exception as e:
        print(f"Erro fatal: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
