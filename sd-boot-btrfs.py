#!/usr/bin/env python3

import btrfsutil, ctypes, os, psutil, re, signal, shutil, sys
from contextlib import contextmanager
from datetime import datetime
from dasbus.connection import SystemMessageBus
from dasbus.unix import GLibClientUnix

# CONFIGURAÇÃO
# =============================================================================
ORIGEM, DESTINO, MANTER = '/', '/snapshots', 4
CONFIG_ORIGEM = '/boot/loader/entries/arch-default.conf'
GER_PKG = ('/usr/bin/yay', '/usr/bin/pacman')
ESP_MNT = '/boot'

# AUXILIARES
# =============================================================================
_libc = ctypes.CDLL('libc.so.6', use_errno=True)
_libc.syncfs.argtypes = [ctypes.c_int]
_libc.syncfs.restype = ctypes.c_int

def syncfs(fd: int):
    if _libc.syncfs(fd) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))

def sync_esp():
    d_fd = os.open(ESP_MNT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        syncfs(d_fd)
    finally:
        os.close(d_fd)

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

def copiar_arquivos_boot(dir_boot):
    if not (os.path.ismount(ESP_MNT) and os.access(ESP_MNT, os.R_OK | os.X_OK)):
        raise RuntimeError(f"{ESP_MNT} está desmontado ou inacessível.")

    os.makedirs(dir_boot, exist_ok=True)

    with open(CONFIG_ORIGEM, 'r') as f:
        linhas = f.readlines()

    for l in linhas:
        l = l.strip().split()
        if l and l[0] in ('linux', 'initrd'):
            src = os.path.join(ESP_MNT, l[1].lstrip('/'))
            if os.path.exists(src):
                # caminho da origem sempre em relação à ESP
                # eventuais subdiretórios na origem são descartados no destino
                shutil.copy2(src, os.path.join(dir_boot, os.path.basename(l[1])))

    sync_esp()

def criar_entrada_boot(snap_nome, ent_conf):
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
        elif l.startswith('linux') or l.startswith('initrd'):
            l = l.split()
            novo_arqv = os.path.join('/snapshots', snap_nome, os.path.basename(l[1]))
            nova_conf.append(f"{l[0]:<9}{novo_arqv}\n")
        else:
            nova_conf.append(f"{l}\n")

    with open(ent_conf, 'w') as f:
        f.writelines(nova_conf)

    sync_esp()

def rotacionar_snapshots(snap_pref, dir_conf):
    with btrfsutil.SubvolumeIterator(DESTINO, info=True) as it:
        snaps = sorted([x for x in it if x[0].startswith(snap_pref)], key=lambda x: x[1].otime)

    snap_mudou = False
    for path, _ in snaps[:-MANTER]:
        snap_remove = os.path.join(DESTINO, path)
        btrfsutil.delete_subvolume(snap_remove)
        snap_mudou = True

        conf_remove = os.path.join(dir_conf, f"{path}.conf")
        if os.path.exists(conf_remove):
            os.remove(conf_remove)

        boot_remove = os.path.join(ESP_MNT, 'snapshots', path)
        if os.path.exists(boot_remove):
            shutil.rmtree(boot_remove)

    if snap_mudou:
        btrfsutil.sync(ORIGEM)
        sync_esp()

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

            snap_base = ORIGEM.strip('/').replace('/', '-')
            snap_pref = f"@{snap_base}_"
            snap_nome = f"{snap_pref}{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            snap_path = os.path.join(DESTINO, snap_nome)
            dir_boot = os.path.join(ESP_MNT, 'snapshots', snap_nome)
            dir_conf = os.path.dirname(CONFIG_ORIGEM)
            ent_conf = os.path.join(dir_conf, f"{snap_nome}.conf")

            btrfsutil.create_snapshot(ORIGEM, snap_path)

            try:
                copiar_arquivos_boot(dir_boot)
                criar_entrada_boot(snap_nome, ent_conf)
                status = 0
            except Exception as e:
                print(f"Desfazendo snapshot {snap_nome}, erro ao criar entrada de inicialização: {e}", file=sys.stderr)
                btrfsutil.delete_subvolume(snap_path)
                btrfsutil.sync(ORIGEM)

                if os.path.exists(ent_conf):
                    os.remove(ent_conf)

                if os.path.exists(dir_boot):
                    shutil.rmtree(dir_boot)

                sync_esp()
                status = 1

            if status == 0:
                rotacionar_snapshots(snap_pref, dir_conf)
            return status

    except Exception as e:
        print(f"Erro fatal: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
