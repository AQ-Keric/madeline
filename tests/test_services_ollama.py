"""Pruebas unitarias y de aislamiento para U-4.3B (OllamaProvider, OllamaCoordinator,
OllamaServerManager y fábrica crear_proveedor_llm).

Cubre:
1. Máquina de estados de OllamaCoordinator (WAITING, ACQUIRED, RUNNING, DRAINING,
   RELEASED), ausencia de fugas ante CancelledError en to_thread y prioridad sobre embeddings.
2. Creación y verificación de unidad transitoria systemd --user (PrivateUsers,
   InaccessiblePaths=/sys/fs/cgroup, KillMode=control-group, mountinfo, uid_map,
   cgroup.procs y socket loopback en puerto dedicado).
3. Propiedad independiente de runners (ppid aunque cambie PGID, cgroup.procs, pidfd
   anti-TOCTOU, sin adopción por nombre).
4. Fallos de systemd y comportamiento fail-closed (NO_DISPONIBLE, sin Popen directo
   ni adopción silenciosa de servidor externo en 11434).
5. Inferencia CPU-only (num_gpu: 0, num_thread: 3, keep_alive: 5m), verificación
   fail-closed en /api/ps (size_vram == 0) y nvidia-smi.
6. Cancelación en streaming con ResourceManager, ciclo de vida/descarga controlada y
   separación estricta frente a servidores externos (DEGRADADO y revalidación explícita).
7. Compatibilidad total con contratos LLM (LLMProvider, LLMRequest, LLMResponse,
   LLMToolCall, rechazo de visión, mapeo de errores) y crear_proveedor_llm.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import httpx

from server.services.llm import (
    EstadoEndpoint,
    EstadoTicket,
    GroqProvider,
    LLMConfigurationError,
    LLMConnectionError,
    LLMMalformedResponseError,
    LLMMessage,
    LLMProvider,
    LLMProviderError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMTimeoutError,
    LLMToolCall,
    LLMToolChoice,
    LLMToolDefinition,
    LLMValidationError,
    OllamaCoordinator,
    OllamaProvider,
    OllamaServerManager,
    SlotTicket,
    crear_proveedor_llm,
)
from server.services.llm.providers.ollama_lifecycle import (
    CommandResult,
    EstadoProceso,
    _default_command_runner,
    _ping_pidfd_vivo,
    _verificar_mountinfo_cgroup_inaccesible,
    _verificar_uid_map_private_users,
    abrir_pidfd_verificado,
    parsear_stat_proc,
    verificar_titularidad_socket,
)
from server.services.resource_manager import (
    DecisionAdmision,
    DecisionContinuidad,
    MetricasRecursos,
    MotivoDecision,
    ResourceManager,
)


def _crear_arbol_proc_cgroup(
    base_dir: str,
    *,
    unit_cg: str = "/user.slice/user-1000.slice/user@1000.service/app.slice/test.service",
    main_pid: int = 5000,
    main_starttime: int = 123456,
    port: int = 11435,
    inodo: int = 998877,
    uid_map_text: str = "      1000       1000          1\n",
    mountinfo_inaccessible: bool = True,
    listen_addr_hex: str = "0100007F",
    net_ns: str = "net:[4026531992]",
    proc_net_ns: str = "net:[4026531992]",
    comm: str = "ollama",
    extra_pids_en_cg: list[tuple[int, int, int, int, str]] | None = None,
) -> tuple[str, str]:
    """Construye un /proc y /sys/fs/cgroup simulados en un directorio temporal."""
    proc_root = os.path.join(base_dir, "proc")
    cgroup_root = os.path.join(base_dir, "cgroup")

    os.makedirs(os.path.join(proc_root, "self", "ns"), exist_ok=True)
    os.makedirs(os.path.join(proc_root, "self", "fdinfo"), exist_ok=True)
    os.makedirs(os.path.join(proc_root, "net"), exist_ok=True)
    os.symlink(net_ns, os.path.join(proc_root, "self", "ns", "net"))

    puerto_hex = f"{port:04X}"
    with open(os.path.join(proc_root, "net", "tcp"), "w", encoding="utf-8") as f:
        f.write("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n")
        f.write(
            f"   0: {listen_addr_hex}:{puerto_hex} 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 {inodo} 1 0\n"
        )

    def _poblar_pid(pid: int, ppid: int, pgrp: int, starttime: int, p_comm: str, con_socket: bool = False) -> None:
        pdir = os.path.join(proc_root, str(pid))
        os.makedirs(os.path.join(pdir, "fd"), exist_ok=True)
        os.makedirs(os.path.join(pdir, "ns"), exist_ok=True)
        os.symlink(proc_net_ns, os.path.join(pdir, "ns", "net"))
        if con_socket:
            os.symlink(f"socket:[{inodo}]", os.path.join(pdir, "fd", "3"))

        # Campos 3..22 post-comm (20 campos: índice 0=state, 1=ppid, 2=pgrp, ..., 19=starttime)
        post = ["S", str(ppid), str(pgrp)] + ["0"] * 16 + [str(starttime)]
        with open(os.path.join(pdir, "stat"), "w", encoding="utf-8") as f:
            f.write(f"{pid} ({p_comm}) {' '.join(post)}\n")
        with open(os.path.join(pdir, "cgroup"), "w", encoding="utf-8") as f:
            f.write(f"0::{unit_cg}\n")
        with open(os.path.join(pdir, "uid_map"), "w", encoding="utf-8") as f:
            f.write(uid_map_text)
        with open(os.path.join(pdir, "mountinfo"), "w", encoding="utf-8") as f:
            f.write("521 492 0:31 / /sys/fs/cgroup rw,nosuid - cgroup2 cgroup2 rw\n")
            if mountinfo_inaccessible:
                f.write("584 521 0:86 /systemd/inaccessible/dir /sys/fs/cgroup ro,nosuid - tmpfs tmpfs rw,mode=700\n")

    _poblar_pid(main_pid, 1153, main_pid, main_starttime, comm, con_socket=True)

    cg_dir = os.path.join(cgroup_root, unit_cg.lstrip("/"))
    os.makedirs(cg_dir, exist_ok=True)
    pids_en_cg = [main_pid]
    if extra_pids_en_cg:
        for ep, eppid, epgrp, est, ecomm in extra_pids_en_cg:
            _poblar_pid(ep, eppid, epgrp, est, ecomm, con_socket=False)
            pids_en_cg.append(ep)

    with open(os.path.join(cg_dir, "cgroup.procs"), "w", encoding="utf-8") as f:
        f.write("\n".join(str(p) for p in pids_en_cg) + "\n")
    with open(os.path.join(cg_dir, "cgroup.events"), "w", encoding="utf-8") as f:
        f.write("populated 1\nfrozen 0\n")

    return proc_root, cgroup_root


class TestOllamaCoordinator(unittest.IsolatedAsyncioTestCase):
    """Pruebas de máquina de estados, concurrencia y cancelación sin fugas en OllamaCoordinator."""

    async def test_exclusion_mutua_real_entre_corutinas_mismo_hilo(self):
        coord = OllamaCoordinator()
        activas = 0
        max_activas = 0
        orden: list[str] = []

        async def tarea(nombre: str) -> None:
            nonlocal activas, max_activas
            async with coord.slot_conversacion(timeout=2.0) as ticket:
                self.assertEqual(ticket.estado, EstadoTicket.ACQUIRED)
                coord.marcar_running(ticket)
                self.assertEqual(ticket.estado, EstadoTicket.RUNNING)
                activas += 1
                max_activas = max(max_activas, activas)
                orden.append(f"start:{nombre}")
                await asyncio.sleep(0.03)
                orden.append(f"end:{nombre}")
                activas -= 1

        await asyncio.gather(tarea("A"), tarea("B"), tarea("C"))
        self.assertEqual(max_activas, 1)
        self.assertFalse(coord.tiene_consumidor_activo())
        self.assertIsNone(coord.estado_actual_snapshot()["titular_id"])

    async def test_cancelacion_en_waiting_no_deja_adquisicion_huerfana(self):
        coord = OllamaCoordinator()
        listo_t1 = asyncio.Event()
        liberar_t1 = asyncio.Event()

        async def titular() -> None:
            async with coord.slot_conversacion(timeout=2.0) as ticket:
                coord.marcar_running(ticket)
                listo_t1.set()
                await liberar_t1.wait()

        t1 = asyncio.create_task(titular())
        await listo_t1.wait()

        async def esperadora() -> None:
            async with coord.slot_conversacion(timeout=2.0):
                self.fail("La tarea cancelada en WAITING no debería entrar al bloque.")

        t2 = asyncio.create_task(esperadora())
        await asyncio.sleep(0.05)  # Asegurar que t2 esté esperando dentro de _adquirir_sync
        t2.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t2

        liberar_t1.set()
        await t1
        # Esperar brevemente a que cualquier hilo del ThreadPool termine su ciclo
        await asyncio.sleep(0.08)

        self.assertFalse(coord.tiene_consumidor_activo())
        # Una tercera conversación debe poder adquirir inmediatamente
        async with coord.slot_conversacion(timeout=0.5) as t3:
            self.assertEqual(t3.estado, EstadoTicket.ACQUIRED)

    async def test_cancelacion_en_acquired_libera_slot_inmediatamente(self):
        coord = OllamaCoordinator()

        async def tarea_acquired() -> None:
            async with coord.slot_conversacion(timeout=1.0) as ticket:
                self.assertEqual(ticket.estado, EstadoTicket.ACQUIRED)
                # Simular cancelación antes de invocar marcar_running (sin HTTP despachado)
                raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await tarea_acquired()

        self.assertFalse(coord.tiene_consumidor_activo())
        async with coord.slot_conversacion(timeout=0.2) as ticket2:
            self.assertEqual(ticket2.estado, EstadoTicket.ACQUIRED)

    async def test_cancelacion_en_running_retiene_slot_en_draining_hasta_limpieza(self):
        coord = OllamaCoordinator()
        en_running = asyncio.Event()
        estado_durante_limpieza: list[object] = []

        async def tarea_running() -> None:
            async with coord.slot_conversacion(timeout=1.0) as ticket:
                coord.marcar_running(ticket)
                en_running.set()
                try:
                    await asyncio.sleep(5.0)
                finally:
                    # Simular limpieza en el llamador tras recibir CancelledError
                    coord.marcar_draining(ticket)
                    estado_durante_limpieza.append(ticket.estado)
                    estado_durante_limpieza.append(coord.tiene_consumidor_activo())
                    await asyncio.sleep(0.05)

        t = asyncio.create_task(tarea_running())
        await en_running.wait()
        t.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t

        self.assertEqual(estado_durante_limpieza, [EstadoTicket.DRAINING, True])
        self.assertFalse(coord.tiene_consumidor_activo())

    async def test_identidad_ticket_impide_liberar_slot_ajeno(self):
        coord = OllamaCoordinator()
        ticket_falso = SlotTicket(id=99999, tipo="conversacion", estado=EstadoTicket.RUNNING, adquirido=True)
        async with coord.slot_conversacion(timeout=1.0) as ticket_real:
            coord.marcar_running(ticket_real)
            self.assertFalse(coord.liberar_slot(ticket_falso))
            self.assertTrue(coord.tiene_consumidor_activo())

    async def test_prioridad_conversacional_y_degradacion_embeddings(self):
        coord = OllamaCoordinator()
        dentro = asyncio.Event()
        salir = asyncio.Event()

        async def conv() -> None:
            async with coord.slot_conversacion(timeout=1.0) as ticket:
                coord.marcar_running(ticket)
                dentro.set()
                await salir.wait()

        t = asyncio.create_task(conv())
        await dentro.wait()

        # Mientras hay conversación activa, un embedding debe ceder rápidamente (None)
        inicio = time.monotonic()
        with coord.slot_embedding(timeout=0.05) as emb_ticket:
            self.assertIsNone(emb_ticket)
        self.assertLess(time.monotonic() - inicio, 0.3)

        salir.set()
        await t

        # Libre y DISPONIBLE: embedding adquiere el slot normalmente
        with coord.slot_embedding(timeout=0.2) as emb_ok:
            self.assertIsNotNone(emb_ok)
            self.assertEqual(emb_ok.estado, EstadoTicket.ACQUIRED)
        self.assertFalse(coord.tiene_consumidor_activo())

        # Si el endpoint pasa a DEGRADADO / NO_DISPONIBLE, ni embeddings ni conversación entran
        coord.marcar_degradado("prueba")
        with coord.slot_embedding(timeout=0.1) as emb_deg:
            self.assertIsNone(emb_deg)
        with self.assertRaises(LLMProviderError):
            async with coord.slot_conversacion(timeout=0.2):
                pass

    async def test_concurrencia_combinada_real_hilos_embeddings_y_corutinas_conversacion(self):
        coord = OllamaCoordinator()
        activas = 0
        max_activas = 0
        lock_metricas = threading.Lock()
        errores: list[Exception] = []
        completados_emb = 0
        completados_conv = 0

        def worker_embedding(idx: int) -> None:
            nonlocal activas, max_activas, completados_emb
            try:
                for _ in range(5):
                    with coord.slot_embedding(timeout=0.05) as tk:
                        if tk is not None:
                            with lock_metricas:
                                activas += 1
                                max_activas = max(max_activas, activas)
                            time.sleep(0.01)
                            with lock_metricas:
                                activas -= 1
                                completados_emb += 1
                    time.sleep(0.005)
            except Exception as exc:
                errores.append(exc)

        async def tarea_conv(idx: int) -> None:
            nonlocal activas, max_activas, completados_conv
            for _ in range(3):
                async with coord.slot_conversacion(timeout=2.0) as tk:
                    coord.marcar_running(tk)
                    with lock_metricas:
                        activas += 1
                        max_activas = max(max_activas, activas)
                    await asyncio.sleep(0.02)
                    with lock_metricas:
                        activas -= 1
                        completados_conv += 1

        hilos = [threading.Thread(target=worker_embedding, args=(i,)) for i in range(4)]
        for h in hilos:
            h.start()
        await asyncio.gather(*(tarea_conv(i) for i in range(4)))
        for h in hilos:
            h.join(timeout=5.0)
            self.assertFalse(h.is_alive())

        self.assertEqual(errores, [])
        self.assertEqual(max_activas, 1)
        self.assertEqual(completados_conv, 12)
        self.assertGreater(completados_emb, 0)
        self.assertFalse(coord.tiene_consumidor_activo())


class TestProcSocketRunnerIsolation(unittest.TestCase):
    """Pruebas de parseo /proc/<pid>/stat, titularidad socket, pidfd y propiedad independiente."""

    def test_parsear_stat_con_espacios_y_parentesis_en_comm(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = os.path.join(tmp, "proc")
            pdir = os.path.join(proc_root, "4242")
            os.makedirs(pdir)
            post = ["S", "100", "4242"] + ["0"] * 16 + ["778899"]
            with open(os.path.join(pdir, "stat"), "w", encoding="utf-8") as f:
                f.write(f"4242 (ollama runner (v2)) {' '.join(post)}\n")

            info = parsear_stat_proc(4242, proc_root=proc_root)
            self.assertIsNotNone(info)
            self.assertEqual(info.comm, "ollama runner (v2)")
            self.assertEqual(info.ppid, 100)
            self.assertEqual(info.pgrp, 4242)
            self.assertEqual(info.starttime, 778899)

    def test_verificar_titularidad_socket_loopback_vs_comodin_y_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root, _ = _crear_arbol_proc_cgroup(tmp, listen_addr_hex="0100007F")
            ok, tipo, inodo = verificar_titularidad_socket(5000, 123456, 11435, proc_root=proc_root)
            self.assertTrue(ok)
            self.assertEqual(tipo, "LOOPBACK_IPV4")
            self.assertEqual(inodo, 998877)

        # Dirección comodín 0.0.0.0 se distingue explícitamente
        with tempfile.TemporaryDirectory() as tmp:
            proc_root, _ = _crear_arbol_proc_cgroup(tmp, listen_addr_hex="00000000")
            ok, tipo, _ = verificar_titularidad_socket(5000, 123456, 11435, proc_root=proc_root)
            self.assertTrue(ok)
            self.assertEqual(tipo, "COMODIN_IPV4")

        # Namespace diferente falla
        with tempfile.TemporaryDirectory() as tmp:
            proc_root, _ = _crear_arbol_proc_cgroup(
                tmp,
                net_ns="net:[1111]",
                proc_net_ns="net:[2222]",
            )
            ok, tipo, _ = verificar_titularidad_socket(5000, 123456, 11435, proc_root=proc_root)
            self.assertFalse(ok)
            self.assertEqual(tipo, "NAMESPACE_DIFERENTE")

    def test_abrir_pidfd_verificado_cierra_fd_si_cambia_starttime_post_apertura(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root, _ = _crear_arbol_proc_cgroup(tmp, main_pid=5000, main_starttime=111)
            fds_cerrados: list[int] = []
            r_fd, w_fd = os.pipe()
            os.close(w_fd)

            def fake_pidfd_open(pid: int, flags: int) -> int:
                # Simular reciclaje de PID exactamente entre el stat previo y la apertura
                pdir = os.path.join(proc_root, str(pid))
                post = ["S", "1", str(pid)] + ["0"] * 16 + ["222"]
                with open(os.path.join(pdir, "stat"), "w", encoding="utf-8") as f:
                    f.write(f"{pid} (otro) {' '.join(post)}\n")
                fds_cerrados.append(r_fd)
                return r_fd

            handle = abrir_pidfd_verificado(
                5000,
                ppid_esperado=1153,
                starttime_esperado=111,
                proc_root=proc_root,
                pidfd_opener=fake_pidfd_open,
            )
            self.assertIsNone(handle)
            # Confirmar que el descriptor abierto fue cerrado (OSError EBADF si se intenta cerrar de nuevo)
            with self.assertRaises(OSError):
                os.close(r_fd)

    def test_descubrir_runners_propiedades_independientes_e_ignora_ajenos(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 5001: hijo directo que cambió de PGID (setsid -> pgrp 5001) y está en cgroup
            # 5002: hijo directo que cambió de PGID y ya no figurase en cgroup (propiedad independiente ppid)
            # 9001: proceso ajeno llamado llama-server con distinto ppid, pgrp y fuera de cgroup
            proc_root, cgroup_root = _crear_arbol_proc_cgroup(
                tmp,
                main_pid=5000,
                main_starttime=123456,
                extra_pids_en_cg=[(5001, 5000, 5001, 123460, "llama-server")],
            )
            # Agregar 5002 (hijo directo con otro pgrp, no en cgroup.procs)
            p5002 = os.path.join(proc_root, "5002")
            os.makedirs(p5002)
            with open(os.path.join(p5002, "stat"), "w", encoding="utf-8") as f:
                post = ["S", "5000", "5002"] + ["0"] * 16 + ["123461"]
                f.write(f"5002 (llama-server) {' '.join(post)}\n")
            # Agregar 9001 (Ollama externo ajeno al árbol)
            p9001 = os.path.join(proc_root, "9001")
            os.makedirs(p9001)
            with open(os.path.join(p9001, "stat"), "w", encoding="utf-8") as f:
                post = ["S", "871", "871"] + ["0"] * 16 + ["9999"]
                f.write(f"9001 (llama-server) {' '.join(post)}\n")

            fake_fds: list[int] = []

            def fake_pidfd(pid: int, flags: int) -> int:
                fd = os.dup(0)
                fake_fds.append(fd)
                return fd

            mgr = OllamaServerManager(
                modo_gestionado=True,
                port=11435,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
                pidfd_opener=fake_pidfd,
                register_atexit=False,
            )
            mgr._main_pid = 5000
            mgr._control_group = "/user.slice/user-1000.slice/user@1000.service/app.slice/test.service"

            runners = mgr.descubrir_runners()
            try:
                self.assertIn(5001, runners)
                self.assertIn(5002, runners)
                self.assertTrue(runners[5002].es_hijo_directo)
                self.assertFalse(runners[5002].mismo_pgrp)
                self.assertNotIn(9001, runners)
            finally:
                mgr._limpiar_runners_fds()

    def test_descubrir_runners_reemplaza_pidfd_si_pid_reciclado(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root, cgroup_root = _crear_arbol_proc_cgroup(
                tmp,
                main_pid=5000,
                main_starttime=123456,
                extra_pids_en_cg=[(5001, 5000, 5000, 200, "llama-server")],
            )
            pipes_creados: list[int] = []

            def make_pipe_fd(pid: int, flags: int) -> int:
                r_fd, w_fd = os.pipe()
                os.close(w_fd)
                pipes_creados.append(r_fd)
                return r_fd

            mgr = OllamaServerManager(
                modo_gestionado=True,
                port=11435,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
                pidfd_opener=make_pipe_fd,
                register_atexit=False,
            )
            mgr._main_pid = 5000
            mgr._control_group = "/user.slice/user-1000.slice/user@1000.service/app.slice/test.service"

            try:
                r1 = mgr.descubrir_runners()
                handle_viejo = r1[5001]
                fd_viejo = handle_viejo.pidfd
                ino_viejo = os.fstat(fd_viejo).st_ino
                count_r1 = len(pipes_creados)
                self.assertEqual(handle_viejo.starttime, 200)

                # Simular reciclaje de PID 5001 con nuevo starttime=999
                with open(os.path.join(proc_root, "5001", "stat"), "w", encoding="utf-8") as f:
                    post = ["S", "5000", "5001"] + ["0"] * 16 + ["999"]
                    f.write(f"5001 (llama-server) {' '.join(post)}\n")

                r2 = mgr.descubrir_runners()
                self.assertIsNot(r2[5001], handle_viejo)
                self.assertIsNone(handle_viejo.pidfd)
                self.assertEqual(r2[5001].starttime, 999)
                self.assertEqual(len(pipes_creados), count_r1 + 1)
                self.assertNotEqual(os.fstat(r2[5001].pidfd).st_ino, ino_viejo)
            finally:
                mgr._limpiar_runners_fds()

    def test_validaciones_estrictas_uid_map_y_mountinfo_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = os.path.join(tmp, "proc")
            pdir = os.path.join(proc_root, "5000")
            os.makedirs(pdir)

            # 1. uid_map malformado o mapeando UID 0 debe fallar (no continue->True)
            for contenido in ("malformed_line\n", "1000 1000 1\nbad\n", "0 1000 1\n", "1000 0 1\n", "1000 1000 5\n", ""):
                with open(os.path.join(pdir, "uid_map"), "w", encoding="utf-8") as f:
                    f.write(contenido)
                self.assertFalse(_verificar_uid_map_private_users(5000, proc_root=proc_root))

            with open(os.path.join(pdir, "uid_map"), "w", encoding="utf-8") as f:
                f.write("      1000       1000          1\n")
            self.assertTrue(_verificar_uid_map_private_users(5000, proc_root=proc_root))

            # 2. mountinfo con montaje posterior rw sobre /sys/fs/cgroup o fstype distinto debe fallar
            with open(os.path.join(pdir, "mountinfo"), "w", encoding="utf-8") as f:
                f.write("584 521 0:86 /systemd/inaccessible/dir /sys/fs/cgroup ro,nosuid - tmpfs tmpfs rw\n")
                f.write("585 584 0:31 / /sys/fs/cgroup rw,nosuid - cgroup2 cgroup2 rw\n")
            self.assertFalse(_verificar_mountinfo_cgroup_inaccesible(5000, proc_root=proc_root))

            with open(os.path.join(pdir, "mountinfo"), "w", encoding="utf-8") as f:
                f.write("linea_sin_separador\n")
            self.assertFalse(_verificar_mountinfo_cgroup_inaccesible(5000, proc_root=proc_root))

            with open(os.path.join(pdir, "mountinfo"), "w", encoding="utf-8") as f:
                f.write("521 492 0:31 / /sys/fs/cgroup rw,nosuid - cgroup2 cgroup2 rw\n")
                f.write("584 521 0:86 /systemd/inaccessible/dir /sys/fs/cgroup ro,nosuid - tmpfs tmpfs rw\n")
            self.assertTrue(_verificar_mountinfo_cgroup_inaccesible(5000, proc_root=proc_root))

    def test_proceso_efimero_real_pidfd_vivo_muerto_y_ausencia_signal(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        handle = None
        try:
            handle = abrir_pidfd_verificado(proc.pid, ppid_esperado=os.getpid())
            self.assertIsNotNone(handle)
            self.assertIsNotNone(handle.pidfd)
            # Proceso real vivo sin mocks de _ping_pidfd_vivo
            self.assertEqual(_ping_pidfd_vivo(handle.pidfd), EstadoProceso.VIVO)

            # Si pidfd_send_signal no existiera, nunca concluye MUERTO: retorna DESCONOCIDO
            with mock.patch("server.services.llm.providers.ollama_lifecycle.signal.pidfd_send_signal", None, create=True):
                self.assertEqual(_ping_pidfd_vivo(handle.pidfd), EstadoProceso.DESCONOCIDO)

            proc.terminate()
            proc.wait(timeout=5.0)
            self.assertEqual(_ping_pidfd_vivo(handle.pidfd), EstadoProceso.MUERTO)
        finally:
            if handle is not None:
                handle.cerrar_fd()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)


class TestOllamaServerManagerSystemd(unittest.TestCase):
    """Pruebas de ciclo de vida systemd --user, fallos fail-closed y limpieza de cgroup."""

    def test_arranque_verificado_detencion_y_vaciado_cgroup(self):
        with tempfile.TemporaryDirectory() as tmp:
            cg_rel = "/user.slice/user-1000.slice/user@1000.service/app.slice/madeline-test.service"
            proc_root, cgroup_root = _crear_arbol_proc_cgroup(
                tmp,
                unit_cg=cg_rel,
                main_pid=5000,
                main_starttime=123456,
                port=11435,
                inodo=998877,
                extra_pids_en_cg=[(5001, 5000, 5001, 123460, "llama-server")],
            )
            comandos: list[list[str]] = []
            detenido = {"flag": False}

            def fake_cmd(cmd, timeout=10.0, env=None):
                comandos.append(list(cmd))
                if cmd[0] == "systemd-run":
                    return CommandResult(0, "", "")
                if cmd[:3] == ["systemctl", "--user", "show"]:
                    if detenido["flag"]:
                        return CommandResult(0, "LoadState=not-found\nActiveState=inactive\nSubState=dead\n", "")
                    return CommandResult(
                        0,
                        (
                            f"Id={cmd[3]}\n"
                            "LoadState=loaded\n"
                            "ActiveState=active\n"
                            "SubState=running\n"
                            f"ControlGroup={cg_rel}\n"
                            "MainPID=5000\n"
                            "PrivateUsers=yes\n"
                            "KillMode=control-group\n"
                            "InaccessiblePaths=/sys/fs/cgroup\n"
                        ),
                        "",
                    )
                if cmd[:3] == ["systemctl", "--user", "stop"]:
                    detenido["flag"] = True
                    # Simular vaciado del cgroup y desaparición de socket LISTEN
                    cg_abs = os.path.join(cgroup_root, cg_rel.lstrip("/"))
                    with open(os.path.join(cg_abs, "cgroup.procs"), "w", encoding="utf-8") as f:
                        f.write("")
                    with open(os.path.join(cg_abs, "cgroup.events"), "w", encoding="utf-8") as f:
                        f.write("populated 0\nfrozen 0\n")
                    with open(os.path.join(proc_root, "net", "tcp"), "w", encoding="utf-8") as f:
                        f.write("  sl local_address rem_address st\n")
                    return CommandResult(0, "", "")
                return CommandResult(1, "", "unknown")

            def fake_pidfd(pid: int, flags: int) -> int:
                return os.dup(0)

            coord = OllamaCoordinator()
            mgr = OllamaServerManager(
                coordinator=coord,
                modo_gestionado=True,
                port=11435,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
                command_runner=fake_cmd,
                pidfd_opener=fake_pidfd,
                health_probe=lambda _url: True,
                register_atexit=False,
            )

            with mock.patch("server.services.llm.providers.ollama_lifecycle._ping_pidfd_vivo", return_value=EstadoProceso.MUERTO):
                mgr.asegurar_servidor_activo()
                self.assertTrue(mgr.aislamiento_verificado)
                self.assertEqual(mgr.main_pid, 5000)
                # Verificar flags obligatorias pasadas a systemd-run
                systemd_run_cmd = comandos[0]
                self.assertEqual(systemd_run_cmd[0:2], ["systemd-run", "--user"])
                self.assertIn("--property=PrivateUsers=yes", systemd_run_cmd)
                self.assertIn("--property=InaccessiblePaths=/sys/fs/cgroup", systemd_run_cmd)
                self.assertIn("--property=KillMode=control-group", systemd_run_cmd)
                self.assertIn("--setenv=CUDA_VISIBLE_DEVICES=", systemd_run_cmd)
                self.assertIn("--setenv=OLLAMA_HOST=127.0.0.1:11435", systemd_run_cmd)

                ok_stop = mgr.detener()
                self.assertTrue(ok_stop)
                self.assertFalse(mgr.aislamiento_verificado)
                self.assertIsNone(mgr.unit_name)

    def test_fallo_systemd_sin_fallback_popen_deja_no_disponible(self):
        def failing_cmd(cmd, timeout=10.0, env=None):
            return CommandResult(1, "", "Failed to connect to user scope bus")

        coord = OllamaCoordinator()
        mgr = OllamaServerManager(
            coordinator=coord,
            modo_gestionado=True,
            port=11435,
            command_runner=failing_cmd,
            register_atexit=False,
        )

        with mock.patch("subprocess.Popen") as popen_mock:
            with self.assertRaises(LLMConnectionError):
                mgr.asegurar_servidor_activo()
            popen_mock.assert_not_called()

        self.assertEqual(coord.estado_endpoint, EstadoEndpoint.NO_DISPONIBLE)
        self.assertFalse(mgr.aislamiento_verificado)

    def test_fallo_mountinfo_inaccessible_paths_aborta_y_marca_no_disponible(self):
        with tempfile.TemporaryDirectory() as tmp:
            cg_rel = "/user.slice/user-1000.slice/user@1000.service/app.slice/madeline-bad.service"
            proc_root, cgroup_root = _crear_arbol_proc_cgroup(
                tmp,
                unit_cg=cg_rel,
                main_pid=5000,
                mountinfo_inaccessible=False,  # Falta overlay /systemd/inaccessible/dir
            )
            paradas: list[str] = []

            def fake_cmd(cmd, timeout=10.0, env=None):
                if cmd[0] == "systemd-run":
                    return CommandResult(0, "", "")
                if cmd[:3] == ["systemctl", "--user", "show"]:
                    return CommandResult(
                        0,
                        (
                            f"Id={cmd[3]}\nLoadState=loaded\nActiveState=active\nSubState=running\n"
                            f"ControlGroup={cg_rel}\nMainPID=5000\nPrivateUsers=yes\n"
                            "KillMode=control-group\nInaccessiblePaths=/sys/fs/cgroup\n"
                        ),
                        "",
                    )
                if cmd[:3] == ["systemctl", "--user", "stop"]:
                    paradas.append(cmd[3])
                    return CommandResult(0, "", "")
                return CommandResult(1, "", "")

            coord = OllamaCoordinator()
            mgr = OllamaServerManager(
                coordinator=coord,
                modo_gestionado=True,
                port=11435,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
                command_runner=fake_cmd,
                startup_timeout_s=0.15,
                stop_timeout_s=0.15,
                register_atexit=False,
            )

            with self.assertRaises(LLMConnectionError) as ctx:
                mgr.asegurar_servidor_activo()
            self.assertIn("mountinfo", str(ctx.exception))
            self.assertEqual(coord.estado_endpoint, EstadoEndpoint.NO_DISPONIBLE)
            self.assertTrue(len(paradas) >= 1)

    def test_detener_con_runner_vivo_marca_degradado_y_retorna_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            cg_rel = "/user.slice/user-1000.slice/user@1000.service/app.slice/madeline-stuck.service"
            proc_root, cgroup_root = _crear_arbol_proc_cgroup(tmp, unit_cg=cg_rel, main_pid=5000)

            def fake_cmd(cmd, timeout=10.0, env=None):
                if cmd[:3] == ["systemctl", "--user", "stop"]:
                    return CommandResult(0, "", "")
                if cmd[:3] == ["systemctl", "--user", "show"]:
                    return CommandResult(0, "LoadState=loaded\nActiveState=deactivating\nSubState=stop-sigkill\n", "")
                return CommandResult(0, "", "")

            coord = OllamaCoordinator()
            mgr = OllamaServerManager(
                coordinator=coord,
                modo_gestionado=True,
                port=11435,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
                command_runner=fake_cmd,
                pidfd_opener=lambda pid, flags: os.dup(0),
                stop_timeout_s=0.1,
                register_atexit=False,
            )
            mgr._unit_name = "madeline-stuck.service"
            mgr._control_group = cg_rel
            mgr._main_pid = 5000
            mgr._aislamiento_verificado = True

            with mock.patch("server.services.llm.providers.ollama_lifecycle._ping_pidfd_vivo", return_value=EstadoProceso.VIVO):
                limpio = mgr.detener()
                self.assertFalse(limpio)
                self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)
                # MEDIO-01: conserva pidfd vivo en _runners y datos de unidad para revalidación posterior
                self.assertIn(5000, mgr._runners)
                self.assertIsNotNone(mgr._runners[5000].pidfd)
                self.assertIsNotNone(mgr.unit_name)

            # Simular cierre posterior exitoso del proceso y cgroup vacío -> revalidar_limpieza recupera
            cg_abs = os.path.join(cgroup_root, cg_rel.lstrip("/"))
            with open(os.path.join(cg_abs, "cgroup.procs"), "w", encoding="utf-8") as f:
                f.write("")
            with open(os.path.join(cg_abs, "cgroup.events"), "w", encoding="utf-8") as f:
                f.write("populated 0\n")
            with open(os.path.join(proc_root, "net", "tcp"), "w", encoding="utf-8") as f:
                f.write("  sl local_address rem_address st\n")
            with mock.patch("server.services.llm.providers.ollama_lifecycle._ping_pidfd_vivo", return_value=EstadoProceso.MUERTO):
                with mock.patch.object(
                    mgr,
                    "_consultar_unidad_systemd",
                    return_value={"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead"},
                ):
                    # Revalidación limpia artefactos del manager pero NO marca DISPONIBLE por sí sola
                    self.assertTrue(mgr.revalidar_limpieza_tras_detencion())
                    self.assertEqual(mgr._runners, {})
                    self.assertIsNone(mgr.unit_name)
                    self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)

                    # Condición incompleta en coordinador (ticket residual / operación) impide recuperar
                    coord._titular_id = 99
                    self.assertFalse(mgr.recuperar_desde_degradado())
                    self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)
                    coord._titular_id = None
                    self.assertFalse(mgr.recuperar_desde_degradado(verificador_operaciones=lambda: False))
                    self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)

                    # Recuperación explícita con todas las condiciones satisfechas
                    self.assertTrue(mgr.recuperar_desde_degradado())
                    self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DISPONIBLE)

            # No debe recuperar si el estado previo fue VIOLACION_GPU o NO_DISPONIBLE
            coord.marcar_degradado("violacion previa")
            mgr._unit_name = "madeline-stuck.service"
            mgr._nvidia_telemetria_estado = "VIOLACION_GPU"
            with mock.patch.object(
                mgr,
                "_consultar_unidad_systemd",
                return_value={"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead"},
            ):
                self.assertTrue(mgr.revalidar_limpieza_tras_detencion())
                self.assertFalse(mgr.recuperar_desde_degradado())
                self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)

            coord.marcar_no_disponible("arranque fallido")
            mgr._nvidia_telemetria_estado = "DESCONOCIDA"
            self.assertFalse(mgr.recuperar_desde_degradado())
            self.assertEqual(coord.estado_endpoint, EstadoEndpoint.NO_DISPONIBLE)

    def test_recuperar_desde_degradado_tras_violacion_gpu_exige_sin_procesos_gpu(self):
        # 1. Sin violación GPU previa, DESCONOCIDA preserva recuperación normal
        coord_norm = OllamaCoordinator()
        coord_norm.marcar_degradado("limpieza lenta")
        mgr_norm = OllamaServerManager(
            coordinator=coord_norm,
            modo_gestionado=True,
            port=11435,
            nvidia_smi_runner=lambda cmd, timeout=3.0: CommandResult(1, "", "no smi"),
            register_atexit=False,
        )
        self.assertTrue(mgr_norm.recuperar_desde_degradado())
        self.assertEqual(coord_norm.estado_endpoint, EstadoEndpoint.DISPONIBLE)

        # 2. Con VIOLACION_GPU previa, DESCONOCIDA, error o salida inválida deben bloquear (fail-closed)
        escenarios_bloqueados = (
            lambda cmd, timeout=3.0: (_ for _ in ()).throw(FileNotFoundError("nvidia-smi")),
            lambda cmd, timeout=3.0: CommandResult(1, "", "NVML failure"),
            lambda cmd, timeout=3.0: CommandResult(0, "invalid csv line\n", ""),
            lambda cmd, timeout=3.0: CommandResult(0, "5000, ollama, 512 MiB\n", ""),
        )
        for runner in escenarios_bloqueados:
            coord = OllamaCoordinator()
            coord.marcar_degradado("violacion gpu")
            mgr = OllamaServerManager(
                coordinator=coord,
                modo_gestionado=True,
                port=11435,
                nvidia_smi_runner=runner,
                register_atexit=False,
            )
            mgr._nvidia_telemetria_estado = "VIOLACION_GPU"
            mgr._main_pid = 5000
            self.assertFalse(mgr.recuperar_desde_degradado(permitir_tras_violacion_gpu=False))
            self.assertFalse(mgr.recuperar_desde_degradado(permitir_tras_violacion_gpu=True))
            self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)

        # 3. Únicamente SIN_PROCESOS_GPU explícito permite recuperar tras VIOLACION_GPU
        coord_ok = OllamaCoordinator()
        coord_ok.marcar_degradado("violacion gpu")
        mgr_ok = OllamaServerManager(
            coordinator=coord_ok,
            modo_gestionado=True,
            port=11435,
            nvidia_smi_runner=lambda cmd, timeout=3.0: CommandResult(0, "9999, Xorg, 40 MiB\n", ""),
            register_atexit=False,
        )
        mgr_ok._nvidia_telemetria_estado = "VIOLACION_GPU"
        mgr_ok._main_pid = 5000
        self.assertTrue(mgr_ok.recuperar_desde_degradado(permitir_tras_violacion_gpu=True))
        self.assertEqual(mgr_ok.nvidia_telemetria_estado, "SIN_PROCESOS_GPU")
        self.assertEqual(coord_ok.estado_endpoint, EstadoEndpoint.DISPONIBLE)

    def test_fallo_uid_map_o_bind_comodin_aborta_fail_closed(self):
        # 1. uid_map mapeando root 0 falla
        with tempfile.TemporaryDirectory() as tmp:
            cg_rel = "/user.slice/user-1000.slice/user@1000.service/app.slice/madeline-uid.service"
            proc_root, cgroup_root = _crear_arbol_proc_cgroup(
                tmp,
                unit_cg=cg_rel,
                main_pid=5000,
                uid_map_text="         0          0          1\n",
            )

            def fake_cmd(cmd, timeout=10.0, env=None):
                if cmd[0] == "systemd-run":
                    return CommandResult(0, "", "")
                if cmd[:3] == ["systemctl", "--user", "show"]:
                    return CommandResult(
                        0,
                        (
                            f"Id={cmd[3]}\nLoadState=loaded\nActiveState=active\nSubState=running\n"
                            f"ControlGroup={cg_rel}\nMainPID=5000\nPrivateUsers=yes\n"
                            "KillMode=control-group\nInaccessiblePaths=/sys/fs/cgroup\n"
                        ),
                        "",
                    )
                if cmd[:3] == ["systemctl", "--user", "stop"]:
                    return CommandResult(0, "", "")
                return CommandResult(0, "", "")

            coord = OllamaCoordinator()
            mgr = OllamaServerManager(
                coordinator=coord,
                modo_gestionado=True,
                port=11435,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
                command_runner=fake_cmd,
                startup_timeout_s=0.1,
                stop_timeout_s=0.1,
                register_atexit=False,
            )
            with self.assertRaises(LLMConnectionError) as ctx:
                mgr.asegurar_servidor_activo()
            self.assertIn("uid_map", str(ctx.exception))
            self.assertEqual(coord.estado_endpoint, EstadoEndpoint.NO_DISPONIBLE)

        # 2. Enlace comodín 0.0.0.0 rechazado para servidor gestionado
        with tempfile.TemporaryDirectory() as tmp:
            cg_rel = "/user.slice/user-1000.slice/user@1000.service/app.slice/madeline-wild.service"
            proc_root, cgroup_root = _crear_arbol_proc_cgroup(
                tmp,
                unit_cg=cg_rel,
                main_pid=5000,
                listen_addr_hex="00000000",
            )

            def fake_cmd_wild(cmd, timeout=10.0, env=None):
                if cmd[0] == "systemd-run":
                    return CommandResult(0, "", "")
                if cmd[:3] == ["systemctl", "--user", "show"]:
                    return CommandResult(
                        0,
                        (
                            f"Id={cmd[3]}\nLoadState=loaded\nActiveState=active\nSubState=running\n"
                            f"ControlGroup={cg_rel}\nMainPID=5000\nPrivateUsers=yes\n"
                            "KillMode=control-group\nInaccessiblePaths=/sys/fs/cgroup\n"
                        ),
                        "",
                    )
                if cmd[:3] == ["systemctl", "--user", "stop"]:
                    return CommandResult(0, "", "")
                return CommandResult(0, "", "")

            coord2 = OllamaCoordinator()
            mgr2 = OllamaServerManager(
                coordinator=coord2,
                modo_gestionado=True,
                port=11435,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
                command_runner=fake_cmd_wild,
                startup_timeout_s=0.1,
                stop_timeout_s=0.1,
                register_atexit=False,
            )
            with self.assertRaises(LLMConnectionError) as ctx2:
                mgr2.asegurar_servidor_activo()
            self.assertIn("COMODIN_IPV4", str(ctx2.exception))
            self.assertEqual(coord2.estado_endpoint, EstadoEndpoint.NO_DISPONIBLE)

    def test_nvidia_smi_con_pid_propio_falla_closed(self):
        mgr = OllamaServerManager(
            modo_gestionado=True,
            port=11435,
            nvidia_smi_runner=lambda cmd, timeout=3.0: CommandResult(0, "5000, ollama, 128 MiB\n", ""),
            register_atexit=False,
        )
        mgr._main_pid = 5000
        with self.assertRaises(LLMProviderError) as ctx:
            mgr.verificar_nvidia_smi_sin_procesos()
        self.assertIn("5000", str(ctx.exception))
        self.assertEqual(mgr.nvidia_telemetria_estado, "VIOLACION_GPU")

    def test_nvidia_smi_runner_predeterminado_y_estados_telemetria(self):
        # 1. Runner predeterminado en instancia de producción es _default_command_runner
        mgr_default = OllamaServerManager(register_atexit=False)
        self.assertIs(mgr_default._nvidia_smi_runner, _default_command_runner)
        self.assertEqual(mgr_default.nvidia_telemetria_estado, "DESCONOCIDA")

        # 2. Binario inexistente -> DESCONOCIDA (nunca SIN_PROCESOS_GPU)
        def raise_fnf(cmd, timeout=3.0):
            raise FileNotFoundError("nvidia-smi")

        mgr_fnf = OllamaServerManager(nvidia_smi_runner=raise_fnf, register_atexit=False)
        self.assertEqual(mgr_fnf.verificar_nvidia_smi_sin_procesos(), "DESCONOCIDA")
        self.assertEqual(mgr_fnf.nvidia_telemetria_estado, "DESCONOCIDA")

        # 3. Error de ejecución -> DESCONOCIDA
        mgr_err = OllamaServerManager(
            nvidia_smi_runner=lambda cmd, timeout=3.0: CommandResult(1, "", "NVML error"),
            register_atexit=False,
        )
        self.assertEqual(mgr_err.verificar_nvidia_smi_sin_procesos(), "DESCONOCIDA")
        self.assertEqual(mgr_err.nvidia_telemetria_estado, "DESCONOCIDA")

        # 4. Salida inválida -> DESCONOCIDA
        mgr_bad = OllamaServerManager(
            nvidia_smi_runner=lambda cmd, timeout=3.0: CommandResult(0, "garbage without csv\n", ""),
            register_atexit=False,
        )
        self.assertEqual(mgr_bad.verificar_nvidia_smi_sin_procesos(), "DESCONOCIDA")
        self.assertEqual(mgr_bad.nvidia_telemetria_estado, "DESCONOCIDA")

        # 4b. Línea malformada antes de PID propio NO debe ocultar VIOLACION_GPU (BAJO-N01)
        mgr_mal_then_own = OllamaServerManager(
            nvidia_smi_runner=lambda cmd, timeout=3.0: CommandResult(
                0,
                "Warning header without csv\n5000, ollama, 512 MiB\n",
                "",
            ),
            register_atexit=False,
        )
        mgr_mal_then_own._main_pid = 5000
        with self.assertRaises(LLMProviderError):
            mgr_mal_then_own.verificar_nvidia_smi_sin_procesos()
        self.assertEqual(mgr_mal_then_own.nvidia_telemetria_estado, "VIOLACION_GPU")

        # 4c. Línea malformada antes de PID ajeno conserva DESCONOCIDA (nunca SIN_PROCESOS_GPU)
        mgr_mal_then_other = OllamaServerManager(
            nvidia_smi_runner=lambda cmd, timeout=3.0: CommandResult(
                0,
                "Warning header without csv\n9999, Xorg, 40 MiB\n",
                "",
            ),
            register_atexit=False,
        )
        mgr_mal_then_other._main_pid = 5000
        self.assertEqual(mgr_mal_then_other.verificar_nvidia_smi_sin_procesos(), "DESCONOCIDA")

        # 4d. PID propio seguido de línea malformada debe priorizar VIOLACION_GPU
        mgr_own_then_mal = OllamaServerManager(
            nvidia_smi_runner=lambda cmd, timeout=3.0: CommandResult(
                0,
                "5000, ollama, 512 MiB\nWarning trailing invalid line\n",
                "",
            ),
            register_atexit=False,
        )
        mgr_own_then_mal._main_pid = 5000
        with self.assertRaises(LLMProviderError):
            mgr_own_then_mal.verificar_nvidia_smi_sin_procesos()
        self.assertEqual(mgr_own_then_mal.nvidia_telemetria_estado, "VIOLACION_GPU")

        # 4e. Varias líneas inválidas sin PID propio -> DESCONOCIDA
        mgr_multi_bad = OllamaServerManager(
            nvidia_smi_runner=lambda cmd, timeout=3.0: CommandResult(
                0,
                "linea invalida 1\nsemicomma, \nmalformed-without-pid\n",
                "",
            ),
            register_atexit=False,
        )
        mgr_multi_bad._main_pid = 5000
        self.assertEqual(mgr_multi_bad.verificar_nvidia_smi_sin_procesos(), "DESCONOCIDA")
        self.assertEqual(mgr_multi_bad.nvidia_telemetria_estado, "DESCONOCIDA")

        # 5. Resultado válido sin procesos propios -> SIN_PROCESOS_GPU
        mgr_ok = OllamaServerManager(
            nvidia_smi_runner=lambda cmd, timeout=3.0: CommandResult(0, "9999, Xorg, 40 MiB\n", ""),
            register_atexit=False,
        )
        mgr_ok._main_pid = 5000
        self.assertEqual(mgr_ok.verificar_nvidia_smi_sin_procesos(), "SIN_PROCESOS_GPU")
        self.assertEqual(mgr_ok.nvidia_telemetria_estado, "SIN_PROCESOS_GPU")

    def test_esperar_arranque_corta_inmediatamente_si_unidad_failed(self):
        paradas: list[str] = []

        def fake_cmd(cmd, timeout=10.0, env=None):
            if cmd[0] == "systemd-run":
                return CommandResult(0, "", "")
            if cmd[:3] == ["systemctl", "--user", "show"]:
                return CommandResult(
                    0,
                    (
                        f"Id={cmd[3]}\nLoadState=loaded\nActiveState=failed\nSubState=failed\n"
                        "Result=exit-code\nExecMainCode=1\nExecMainStatus=2\nControlGroup=\nMainPID=0\n"
                    ),
                    "",
                )
            if cmd[:3] == ["systemctl", "--user", "stop"]:
                paradas.append(cmd[3])
                return CommandResult(0, "", "")
            return CommandResult(0, "", "")

        coord = OllamaCoordinator()
        mgr = OllamaServerManager(
            coordinator=coord,
            modo_gestionado=True,
            port=11435,
            command_runner=fake_cmd,
            startup_timeout_s=5.0,  # Timeout largo; debe abortar en < 0.5 s
            stop_timeout_s=0.1,
            register_atexit=False,
        )
        t0 = time.monotonic()
        with self.assertRaises(LLMConnectionError) as ctx:
            mgr.asegurar_servidor_activo()
        duracion = time.monotonic() - t0

        self.assertLess(duracion, 0.8)
        self.assertIn("ActiveState='failed'", str(ctx.exception))
        self.assertIn("ExecMainStatus=2", mgr.ultimo_error_arranque)
        self.assertEqual(coord.estado_endpoint, EstadoEndpoint.NO_DISPONIBLE)
        self.assertTrue(len(paradas) >= 1)

    def test_cierre_concurrente_detener_y_atexit_sin_deadlock(self):
        with tempfile.TemporaryDirectory() as tmp:
            cg_rel = "/user.slice/user-1000.slice/user@1000.service/app.slice/madeline-conc.service"
            proc_root, cgroup_root = _crear_arbol_proc_cgroup(tmp, unit_cg=cg_rel, main_pid=5000)

            def fake_cmd(cmd, timeout=10.0, env=None):
                if cmd[:3] == ["systemctl", "--user", "stop"]:
                    time.sleep(0.05)
                    cg_abs = os.path.join(cgroup_root, cg_rel.lstrip("/"))
                    with open(os.path.join(cg_abs, "cgroup.procs"), "w", encoding="utf-8") as f:
                        f.write("")
                    with open(os.path.join(cg_abs, "cgroup.events"), "w", encoding="utf-8") as f:
                        f.write("populated 0\n")
                    with open(os.path.join(proc_root, "net", "tcp"), "w", encoding="utf-8") as f:
                        f.write("  sl local_address rem_address st\n")
                    return CommandResult(0, "", "")
                if cmd[:3] == ["systemctl", "--user", "show"]:
                    return CommandResult(0, "LoadState=not-found\nActiveState=inactive\nSubState=dead\n", "")
                return CommandResult(0, "", "")

            mgr = OllamaServerManager(
                modo_gestionado=True,
                port=11435,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
                command_runner=fake_cmd,
                pidfd_opener=lambda pid, fl: os.dup(0),
                stop_timeout_s=1.0,
                register_atexit=False,
            )
            mgr._unit_name = "madeline-conc.service"
            mgr._control_group = cg_rel
            mgr._main_pid = 5000
            mgr._inodo_socket_original = 998877

            errores: list[Exception] = []
            resultados: list[bool] = []

            def run_detener():
                try:
                    resultados.append(mgr.detener())
                except Exception as exc:
                    errores.append(exc)

            with mock.patch("server.services.llm.providers.ollama_lifecycle._ping_pidfd_vivo", return_value=EstadoProceso.MUERTO):
                t1 = threading.Thread(target=run_detener)
                t2 = threading.Thread(target=mgr._atexit_cleanup)
                t3 = threading.Thread(target=lambda: resultados.append(asyncio.run(mgr.aclose())))
                t1.start()
                t2.start()
                t3.start()
                t1.join(timeout=3.0)
                t2.join(timeout=3.0)
                t3.join(timeout=3.0)

            self.assertEqual(errores, [])
            self.assertEqual(len(resultados), 2)
            self.assertTrue(all(resultados))
            self.assertIsNone(mgr.unit_name)

    def test_arranque_concurrente_serializado_con_shutdown_lock_y_aclose(self):
        with tempfile.TemporaryDirectory() as tmp:
            cg_rel = "/user.slice/user-1000.slice/user@1000.service/app.slice/madeline-lock.service"
            proc_root, cgroup_root = _crear_arbol_proc_cgroup(
                tmp,
                unit_cg=cg_rel,
                main_pid=5000,
                main_starttime=123456,
                port=11435,
            )
            arranque_en_curso = threading.Event()
            liberar_arranque = threading.Event()
            systemd_run_unidades: list[str] = []
            detenido = {"flag": False}

            def fake_cmd(cmd, timeout=10.0, env=None):
                if cmd[0] == "systemd-run":
                    systemd_run_unidades.append(cmd[2])
                    arranque_en_curso.set()
                    liberar_arranque.wait(timeout=3.0)
                    detenido["flag"] = False
                    return CommandResult(0, "", "")
                if cmd[:3] == ["systemctl", "--user", "show"]:
                    if detenido["flag"]:
                        return CommandResult(0, "LoadState=not-found\nActiveState=inactive\nSubState=dead\n", "")
                    return CommandResult(
                        0,
                        (
                            f"Id={cmd[3]}\nLoadState=loaded\nActiveState=active\nSubState=running\n"
                            f"ControlGroup={cg_rel}\nMainPID=5000\nPrivateUsers=yes\n"
                            "KillMode=control-group\nInaccessiblePaths=/sys/fs/cgroup\n"
                        ),
                        "",
                    )
                if cmd[:3] == ["systemctl", "--user", "stop"]:
                    detenido["flag"] = True
                    cg_abs = os.path.join(cgroup_root, cg_rel.lstrip("/"))
                    with open(os.path.join(cg_abs, "cgroup.procs"), "w", encoding="utf-8") as f:
                        f.write("")
                    with open(os.path.join(cg_abs, "cgroup.events"), "w", encoding="utf-8") as f:
                        f.write("populated 0\n")
                    with open(os.path.join(proc_root, "net", "tcp"), "w", encoding="utf-8") as f:
                        f.write("  sl local_address rem_address st\n")
                    return CommandResult(0, "", "")
                return CommandResult(0, "", "")

            coord = OllamaCoordinator()
            mgr = OllamaServerManager(
                coordinator=coord,
                modo_gestionado=True,
                port=11435,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
                command_runner=fake_cmd,
                pidfd_opener=lambda pid, flags: os.dup(0),
                health_probe=lambda _url: True,
                register_atexit=False,
            )

            errores: list[Exception] = []
            stop_result: list[bool] = []
            t1 = threading.Thread(target=mgr.asegurar_servidor_activo)
            t2 = threading.Thread(target=mgr.asegurar_servidor_activo)
            t_stop = threading.Thread(target=lambda: stop_result.append(mgr.detener()))

            with mock.patch("server.services.llm.providers.ollama_lifecycle._ping_pidfd_vivo", return_value=EstadoProceso.MUERTO):
                t1.start()
                self.assertTrue(arranque_en_curso.wait(timeout=2.0))
                t2.start()
                t_stop.start()
                time.sleep(0.05)
                # Mientras t1 mantiene _shutdown_lock, no puede crearse una segunda unidad
                self.assertEqual(len(systemd_run_unidades), 1)
                self.assertTrue(t2.is_alive())
                self.assertTrue(t_stop.is_alive())

                liberar_arranque.set()
                t1.join(timeout=3.0)
                t2.join(timeout=3.0)
                t_stop.join(timeout=3.0)

            self.assertEqual(errores, [])
            self.assertEqual(len(systemd_run_unidades), 1)
            self.assertEqual(stop_result, [True])
            self.assertIsNone(mgr.unit_name)


class TestOllamaProviderContractsAndExecution(unittest.IsolatedAsyncioTestCase):
    """Pruebas del adaptador OllamaProvider, contratos LLM, GPU fail-closed y ResourceManager."""

    def _crear_mock_transport(
        self,
        chat_lines: list[dict],
        ps_payload: dict | list[dict] | None = None,
        chat_status: int = 200,
        ps_status: int = 200,
        ps_raises: Exception | None = None,
        on_chat_payload: list[dict] | None = None,
        on_generate_payload: list[dict] | None = None,
        tags_status: int = 200,
    ) -> httpx.MockTransport:
        ps_iter = iter(ps_payload) if isinstance(ps_payload, list) else None

        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/api/chat":
                if on_chat_payload is not None:
                    on_chat_payload.append(json.loads(request.content.decode("utf-8")))
                if chat_status >= 400:
                    return httpx.Response(chat_status, json={"error": "chat failed"})
                body = "\n".join(json.dumps(item) for item in chat_lines) + "\n"
                return httpx.Response(chat_status, content=body.encode("utf-8"))
            if path == "/api/ps":
                if ps_raises is not None:
                    raise ps_raises
                if ps_status >= 400:
                    return httpx.Response(ps_status, json={"error": "ps failed"})
                payload = next(ps_iter) if ps_iter is not None else (
                    ps_payload if ps_payload is not None else {"models": [{"model": "qwen2.5:3b", "size_vram": 0}]}
                )
                return httpx.Response(200, json=payload)
            if path == "/api/tags":
                return httpx.Response(tags_status, json={"models": []})
            if path == "/api/generate":
                if on_generate_payload is not None:
                    on_generate_payload.append(json.loads(request.content.decode("utf-8")))
                return httpx.Response(200, json={"done": True})
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    async def test_es_llm_provider_y_rechaza_vision(self):
        mgr = mock.MagicMock(spec=OllamaServerManager)
        mgr.coordinator = OllamaCoordinator()
        mgr.base_url = "http://127.0.0.1:11435"
        mgr.modo_gestionado = True
        prov = OllamaProvider(server_manager=mgr)

        self.assertIsInstance(prov, LLMProvider)
        self.assertEqual(prov.nombre, "ollama")
        with self.assertRaises(LLMProviderError) as ctx:
            await prov.analizar_vision(b"pngbytes", "png", "¿Qué ves?")
        self.assertIn("Groq", str(ctx.exception))

    async def test_generar_respuesta_cpu_only_payload_y_tool_calls(self):
        captured_chat: list[dict] = []
        chat_lines = [
            {"model": "qwen2.5:3b", "message": {"role": "assistant", "content": "Voy a "}, "done": False},
            {
                "model": "qwen2.5:3b",
                "message": {
                    "role": "assistant",
                    "content": "revisar.",
                    "tool_calls": [
                        {"id": "call_1", "function": {"name": "ver_hora", "arguments": {"tz": "UTC"}}}
                    ],
                },
                "done": True,
                "done_reason": "stop",
            },
        ]
        transport = self._crear_mock_transport(chat_lines=chat_lines, on_chat_payload=captured_chat)
        client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:11435")

        mgr = mock.MagicMock(spec=OllamaServerManager)
        mgr.coordinator = OllamaCoordinator()
        mgr.base_url = "http://127.0.0.1:11435"
        mgr.modo_gestionado = True

        prov = OllamaProvider(
            server_manager=mgr,
            http_client=client,
            num_thread=3,
            keep_alive="5m",
        )
        req = LLMRequest(
            mensajes=[LLMMessage.system("Sos Madeline"), LLMMessage.user("Qué hora es?")],
            tools=[LLMToolDefinition(nombre="ver_hora", descripcion="Obtiene hora", parametros={"type": "object"})],
            tool_choice=LLMToolChoice.auto(),
            temperature=0.2,
            max_tokens=120,
            reasoning_effort="low",  # Debe ignorarse sin fallar
        )

        resp = await prov.generar_respuesta(req)
        await client.aclose()

        self.assertIsInstance(resp, LLMResponse)
        self.assertEqual(resp.texto, "Voy a revisar.")
        self.assertEqual(resp.finish_reason, "stop")
        self.assertEqual(resp.modelo, "qwen2.5:3b")
        self.assertEqual(resp.proveedor, "ollama")
        self.assertEqual(len(resp.tool_calls), 1)
        self.assertEqual(resp.tool_calls[0].id, "call_1")
        self.assertEqual(resp.tool_calls[0].nombre, "ver_hora")
        self.assertEqual(resp.tool_calls[0].argumentos, {"tz": "UTC"})

        sent = captured_chat[0]
        self.assertEqual(sent["options"]["num_gpu"], 0)
        self.assertEqual(sent["options"]["num_thread"], 3)
        self.assertEqual(sent["options"]["temperature"], 0.2)
        self.assertEqual(sent["options"]["num_predict"], 120)
        self.assertEqual(sent["keep_alive"], "5m")
        self.assertTrue(sent["stream"])
        self.assertEqual(len(sent["tools"]), 1)

    async def test_api_ps_size_vram_mayor_que_cero_aborta_fail_closed_y_detiene_unidad(self):
        chat_lines = [
            {"model": "qwen2.5:3b", "message": {"role": "assistant", "content": "hola"}, "done": False},
            {"model": "qwen2.5:3b", "message": {"role": "assistant", "content": " mundo"}, "done": True, "done_reason": "stop"},
        ]
        transport = self._crear_mock_transport(
            chat_lines=chat_lines,
            ps_payload={"models": [{"model": "qwen2.5:3b", "size_vram": 512000}]},
        )
        client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:11435")

        coord = OllamaCoordinator()
        mgr = mock.MagicMock(spec=OllamaServerManager)
        mgr.coordinator = coord
        mgr.base_url = "http://127.0.0.1:11435"
        mgr.modo_gestionado = True
        mgr.detener.return_value = True

        prov = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=client)
        with self.assertRaises(LLMProviderError) as ctx:
            await prov.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hola")]))
        await client.aclose()

        self.assertIn("size_vram=512000", str(ctx.exception))
        mgr.detener.assert_called_once()
        self.assertFalse(coord.tiene_consumidor_activo())

    async def test_api_ps_lista_vacia_o_timeout_falla_closed(self):
        for ps_cfg in (
            {"ps_payload": {"models": []}, "expected": "no figura en /api/ps"},
            {"ps_raises": httpx.ReadTimeout("ps timeout"), "expected": "/api/ps no respondió"},
        ):
            chat_lines = [
                {"model": "qwen2.5:3b", "message": {"role": "assistant", "content": "x"}, "done": True}
            ]
            transport = self._crear_mock_transport(
                chat_lines=chat_lines,
                ps_payload=ps_cfg.get("ps_payload"),
                ps_raises=ps_cfg.get("ps_raises"),
            )
            client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:11435")
            coord = OllamaCoordinator()
            mgr = mock.MagicMock(spec=OllamaServerManager)
            mgr.coordinator = coord
            mgr.base_url = "http://127.0.0.1:11435"
            mgr.modo_gestionado = True
            mgr.detener.return_value = True

            prov = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=client)
            with self.assertRaises(LLMProviderError) as ctx:
                await prov.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hola")]))
            await client.aclose()
            self.assertIn(ps_cfg["expected"], str(ctx.exception))
            mgr.detener.assert_called_once()

    async def test_resource_manager_deniega_admision_y_cancela_en_streaming(self):
        # 1. Rechazo en admisión (ej. juego activo)
        rm_denegar = ResourceManager(
            fallback_habilitado=True,
            perfil_inicial="JUEGO",
            lector_sensores=lambda: {"ram_total": 16000, "ram_usada": 8000, "cpu_uso": 20.0, "gpu_uso": 0.0},
            lector_ventana_activa=lambda: None,
            lector_clientes=lambda: [],
            lector_procesos=lambda: None,
        )
        mgr = mock.MagicMock(spec=OllamaServerManager)
        mgr.coordinator = OllamaCoordinator()
        mgr.base_url = "http://127.0.0.1:11435"
        mgr.modo_gestionado = True
        prov_den = OllamaProvider(server_manager=mgr, resource_manager=rm_denegar)

        with self.assertRaises(LLMProviderError) as ctx:
            await prov_den.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hola")]))
        self.assertIn("PERFIL_JUEGO", str(ctx.exception))
        mgr.asegurar_servidor_activo.assert_not_called()

        # 2. Cancelación en vuelo durante streaming
        rm_vivo = ResourceManager(
            fallback_habilitado=True,
            perfil_inicial="DEFAULT",
            lector_sensores=lambda: {"ram_total": 16000, "ram_usada": 8000, "cpu_uso": 20.0, "gpu_uso": 0.0},
            lector_ventana_activa=lambda: None,
            lector_clientes=lambda: [],
            lector_procesos=lambda: None,
        )

        async def _stream_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": [{"model": "qwen2.5:3b", "size_vram": 0}]})
            if request.url.path == "/api/chat":
                # Al recibir primer chunk, activar perfil JUEGO para forzar cancelación de continuidad
                rm_vivo.fijar_perfil("JUEGO")
                body = json.dumps({"model": "qwen2.5:3b", "message": {"content": "parte 1"}, "done": False}) + "\n"
                return httpx.Response(200, content=body.encode("utf-8"))
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(_stream_handler), base_url="http://127.0.0.1:11435")
        prov_cancel = OllamaProvider(
            server_manager=mgr,
            coordinator=mgr.coordinator,
            resource_manager=rm_vivo,
            http_client=client,
            intervalo_continuidad_s=0.0,
        )
        with self.assertRaises(LLMProviderError) as ctx2:
            await prov_cancel.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hola")]))
        await client.aclose()
        self.assertIn("PERFIL_JUEGO", str(ctx2.exception))
        mgr.detener.assert_called_once()
        self.assertFalse(mgr.coordinator.tiene_consumidor_activo())

    async def test_servidor_externo_sin_descarga_forzada_y_revalidacion_explicita(self):
        generated_payloads: list[dict] = []
        transport = self._crear_mock_transport(
            chat_lines=[{"model": "qwen2.5:3b", "message": {"content": "chunk"}, "done": False}],  # Sin done:true -> aborta
            on_generate_payload=generated_payloads,
        )
        client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:11434")

        coord = OllamaCoordinator()
        mgr = OllamaServerManager(
            coordinator=coord,
            modo_gestionado=False,
            port=11434,
            register_atexit=False,
        )
        prov = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=client)

        with self.assertRaises(LLMMalformedResponseError):
            await prov.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hola")]))

        # Debe marcarse DEGRADADO y prohibir descargas forzadas
        self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)
        self.assertEqual(generated_payloads, [])

        with self.assertRaises(LLMProviderError):
            await prov.descargar_modelo()

        # Un ping sin confirmación explícita NO rehabilita
        self.assertFalse(await prov.revalidar_proveedor_externo(confirmacion_usuario=False))
        self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)

        # Con confirmación explícita y /api/tags + /api/ps válidos sí rehabilita
        self.assertTrue(await prov.revalidar_proveedor_externo(confirmacion_usuario=True))
        self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DISPONIBLE)
        await client.aclose()

    async def test_descarga_modelo_gestionado_solo_cuando_inactivo(self):
        generated_payloads: list[dict] = []
        transport = self._crear_mock_transport(chat_lines=[], on_generate_payload=generated_payloads)
        client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:11435")

        coord = OllamaCoordinator()
        mgr = mock.MagicMock(spec=OllamaServerManager)
        mgr.coordinator = coord
        mgr.base_url = "http://127.0.0.1:11435"
        mgr.modo_gestionado = True
        prov = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=client)

        ok = await prov.descargar_modelo()
        self.assertTrue(ok)
        self.assertEqual(generated_payloads, [{"model": "qwen2.5:3b", "keep_alive": 0}])

        # Si hay consumidor activo, rechaza descarga
        async with coord.slot_conversacion(timeout=1.0):
            with self.assertRaises(LLMProviderError):
                await prov.descargar_modelo()
        await client.aclose()

    async def test_mapeo_errores_y_respuestas_malformadas(self):
        coord = OllamaCoordinator()
        mgr = mock.MagicMock(spec=OllamaServerManager)
        mgr.coordinator = coord
        mgr.base_url = "http://127.0.0.1:11435"
        mgr.modo_gestionado = True
        mgr.detener.return_value = True

        # 1. Timeout en transporte
        async def timeout_handler(_req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timeout lectura")

        c1 = httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler), base_url="http://127.0.0.1:11435")
        p1 = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=c1)
        with self.assertRaises(LLMTimeoutError) as ctx1:
            await p1.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hi")]))
        self.assertIsNotNone(ctx1.exception.__cause__)
        await c1.aclose()

        # 2. ConnectError -> LLMConnectionError
        async def conn_handler(_req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        coord.marcar_disponible()
        c2 = httpx.AsyncClient(transport=httpx.MockTransport(conn_handler), base_url="http://127.0.0.1:11435")
        p2 = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=c2)
        with self.assertRaises(LLMConnectionError) as ctx2:
            await p2.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hi")]))
        self.assertIsNotNone(ctx2.exception.__cause__)
        await c2.aclose()

        # 3. HTTP 429 -> LLMRateLimitError (error HTTP completo, no dispara detener())
        mgr.detener.reset_mock()
        coord.marcar_disponible()
        t3 = self._crear_mock_transport(chat_lines=[], chat_status=429)
        c3 = httpx.AsyncClient(transport=t3, base_url="http://127.0.0.1:11435")
        p3 = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=c3)
        with self.assertRaises(LLMRateLimitError):
            await p3.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hi")]))
        mgr.detener.assert_not_called()
        await c3.aclose()

        # 4. Tool call sin ID -> LLMMalformedResponseError
        coord.marcar_disponible()
        t4 = self._crear_mock_transport(
            chat_lines=[
                {
                    "model": "qwen2.5:3b",
                    "message": {"content": None, "tool_calls": [{"function": {"name": "fn", "arguments": {}}}]},
                    "done": True,
                }
            ]
        )
        c4 = httpx.AsyncClient(transport=t4, base_url="http://127.0.0.1:11435")
        p4 = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=c4)
        with self.assertRaises(LLMMalformedResponseError):
            await p4.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hi")]))
        await c4.aclose()

    async def test_cancelacion_asyncio_mid_stream_detiene_unidad_y_libera_ticket(self):
        coord = OllamaCoordinator()
        mgr = mock.MagicMock(spec=OllamaServerManager)
        mgr.coordinator = coord
        mgr.base_url = "http://127.0.0.1:11435"
        mgr.modo_gestionado = True
        mgr.detener.return_value = True

        primer_chunk_emitido = asyncio.Event()

        class _SlowAsyncStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield (json.dumps({"model": "qwen2.5:3b", "message": {"content": "hola"}, "done": False}) + "\n").encode()
                primer_chunk_emitido.set()
                await asyncio.sleep(5.0)

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": [{"model": "qwen2.5:3b", "size_vram": 0}]})
            if request.url.path == "/api/chat":
                return httpx.Response(200, stream=_SlowAsyncStream())
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11435")
        prov = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=client)

        tarea = asyncio.create_task(prov.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hola")])))
        await primer_chunk_emitido.wait()
        tarea.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await tarea

        await client.aclose()
        mgr.detener.assert_called_once()
        self.assertFalse(coord.tiene_consumidor_activo())
        self.assertEqual(coord.estado_actual_snapshot()["ticket_estado"], None)

    async def test_cancelaciones_repetidas_durante_draining_no_liberan_prematuramente(self):
        coord = OllamaCoordinator()
        detener_iniciado = threading.Event()
        permitir_fin_detener = threading.Event()
        estados_durante_detener: list[tuple[bool, EstadoEndpoint]] = []

        mgr = mock.MagicMock(spec=OllamaServerManager)
        mgr.coordinator = coord
        mgr.base_url = "http://127.0.0.1:11435"
        mgr.modo_gestionado = True
        mgr.nvidia_telemetria_estado = "SIN_PROCESOS_GPU"

        def detener_lento() -> bool:
            detener_iniciado.set()
            estados_durante_detener.append((coord.tiene_consumidor_activo(), coord.estado_endpoint))
            permitir_fin_detener.wait(timeout=3.0)
            return True

        mgr.detener.side_effect = detener_lento
        mgr.revalidar_limpieza_tras_detencion.return_value = True

        primer_chunk = asyncio.Event()

        class _SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield (json.dumps({"model": "qwen2.5:3b", "message": {"content": "hola"}, "done": False}) + "\n").encode()
                primer_chunk.set()
                await asyncio.sleep(5.0)

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": [{"model": "qwen2.5:3b", "size_vram": 0}]})
            if request.url.path == "/api/chat":
                return httpx.Response(200, stream=_SlowStream())
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11435")
        prov = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=client)

        tarea = asyncio.create_task(prov.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hola")])))
        await primer_chunk.wait()

        # 1ª cancelación -> entra en DRAINING y marca DEGRADADO preventivamente
        tarea.cancel()
        await asyncio.to_thread(detener_iniciado.wait, 2.0)

        # 2ª y 3ª cancelaciones repetidas mientras detener() sigue corriendo
        tarea.cancel()
        await asyncio.sleep(0.02)
        tarea.cancel()
        await asyncio.sleep(0.02)

        # Ni el slot ni el endpoint deben haberse liberado prematuramente
        self.assertTrue(coord.tiene_consumidor_activo())
        self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)
        self.assertFalse(tarea.done())

        permitir_fin_detener.set()
        with self.assertRaises(asyncio.CancelledError):
            await tarea

        self.assertEqual(estados_durante_detener, [(True, EstadoEndpoint.DEGRADADO)])
        self.assertFalse(coord.tiene_consumidor_activo())
        self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DISPONIBLE)
        await prov.aclose()
        await client.aclose()

    async def test_draining_conserva_degradado_si_detener_falla_o_incompleto(self):
        coord = OllamaCoordinator()
        mgr = mock.MagicMock(spec=OllamaServerManager)
        mgr.coordinator = coord
        mgr.base_url = "http://127.0.0.1:11435"
        mgr.modo_gestionado = True
        mgr.detener.return_value = False

        primer_chunk = asyncio.Event()

        class _SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield (json.dumps({"model": "qwen2.5:3b", "message": {"content": "hola"}, "done": False}) + "\n").encode()
                primer_chunk.set()
                await asyncio.sleep(5.0)

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": [{"model": "qwen2.5:3b", "size_vram": 0}]})
            if request.url.path == "/api/chat":
                return httpx.Response(200, stream=_SlowStream())
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11435")
        prov = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=client)

        tarea = asyncio.create_task(prov.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hola")])))
        await primer_chunk.wait()
        tarea.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tarea

        self.assertFalse(coord.tiene_consumidor_activo())
        self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)
        await client.aclose()

    async def test_cancelacion_en_acquired_durante_arranque_protege_slot_unidad_y_aclose(self):
        with tempfile.TemporaryDirectory() as tmp:
            cg_rel = "/user.slice/user-1000.slice/user@1000.service/app.slice/madeline-acq.service"
            proc_root, cgroup_root = _crear_arbol_proc_cgroup(
                tmp,
                unit_cg=cg_rel,
                main_pid=5000,
                main_starttime=123456,
                port=11435,
            )
            arranque_iniciado = threading.Event()
            permitir_fin_arranque = threading.Event()
            detenido = {"flag": False}
            systemd_runs: list[str] = []
            stops: list[str] = []

            def fake_cmd(cmd, timeout=10.0, env=None):
                if cmd[0] == "systemd-run":
                    systemd_runs.append(cmd[2])
                    arranque_iniciado.set()
                    permitir_fin_arranque.wait(timeout=3.0)
                    detenido["flag"] = False
                    return CommandResult(0, "", "")
                if cmd[:3] == ["systemctl", "--user", "show"]:
                    if detenido["flag"]:
                        return CommandResult(0, "LoadState=not-found\nActiveState=inactive\nSubState=dead\n", "")
                    return CommandResult(
                        0,
                        (
                            f"Id={cmd[3]}\nLoadState=loaded\nActiveState=active\nSubState=running\n"
                            f"ControlGroup={cg_rel}\nMainPID=5000\nPrivateUsers=yes\n"
                            "KillMode=control-group\nInaccessiblePaths=/sys/fs/cgroup\n"
                        ),
                        "",
                    )
                if cmd[:3] == ["systemctl", "--user", "stop"]:
                    stops.append(cmd[3])
                    detenido["flag"] = True
                    cg_abs = os.path.join(cgroup_root, cg_rel.lstrip("/"))
                    with open(os.path.join(cg_abs, "cgroup.procs"), "w", encoding="utf-8") as f:
                        f.write("")
                    with open(os.path.join(cg_abs, "cgroup.events"), "w", encoding="utf-8") as f:
                        f.write("populated 0\n")
                    with open(os.path.join(proc_root, "net", "tcp"), "w", encoding="utf-8") as f:
                        f.write("  sl local_address rem_address st\n")
                    return CommandResult(0, "", "")
                return CommandResult(0, "", "")

            coord = OllamaCoordinator()
            mgr = OllamaServerManager(
                coordinator=coord,
                modo_gestionado=True,
                port=11435,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
                command_runner=fake_cmd,
                nvidia_smi_runner=lambda cmd, timeout=3.0: CommandResult(0, "", ""),
                pidfd_opener=lambda pid, flags: os.dup(0),
                health_probe=lambda _url: True,
                register_atexit=False,
            )
            transport = self._crear_mock_transport(
                chat_lines=[{"model": "qwen2.5:3b", "message": {"content": "ok"}, "done": True}]
            )
            client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:11435")
            prov = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=client)

            with mock.patch("server.services.llm.providers.ollama_lifecycle._ping_pidfd_vivo", return_value=EstadoProceso.MUERTO):
                t1 = asyncio.create_task(prov.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("u1")])))
                self.assertTrue(await asyncio.to_thread(arranque_iniciado.wait, 2.0))

                # Cancelar repetidamente durante ACQUIRED no libera slot mientras el hilo sigue arrancando
                t1.cancel()
                await asyncio.sleep(0.02)
                t1.cancel()
                # Segunda solicitud concurrente y aclose() deben esperar a que t1 resuelva arranque y limpieza
                t2 = asyncio.create_task(prov.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("u2")])))
                t_close = asyncio.create_task(prov.aclose())
                await asyncio.sleep(0.02)

                self.assertTrue(coord.tiene_consumidor_activo())
                self.assertFalse(t1.done())
                self.assertFalse(t2.done())
                self.assertFalse(t_close.done())
                self.assertEqual(len(systemd_runs), 1)

                permitir_fin_arranque.set()
                with self.assertRaises(asyncio.CancelledError):
                    await t1
                # t2 entra después de completarse la limpieza de t1 y falla limpio porque aclose() marcó cierre
                with self.assertRaises(LLMProviderError):
                    await t2
                await t_close

            self.assertEqual(len(systemd_runs), 1)
            self.assertGreaterEqual(len(stops), 1)
            self.assertIsNone(mgr.unit_name)
            self.assertFalse(coord.tiene_consumidor_activo())
            self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DISPONIBLE)
            self.assertEqual(prov._tareas_arranque, set())
            self.assertEqual(prov._tareas_drenaje, set())
            self.assertEqual(prov._operaciones_arranque, set())
            await client.aclose()

    async def test_segunda_solicitud_concurrente_durante_arranque_reutiliza_unica_unidad(self):
        with tempfile.TemporaryDirectory() as tmp:
            cg_rel = "/user.slice/user-1000.slice/user@1000.service/app.slice/madeline-uniq.service"
            proc_root, cgroup_root = _crear_arbol_proc_cgroup(
                tmp,
                unit_cg=cg_rel,
                main_pid=5000,
                main_starttime=123456,
                port=11435,
            )
            arranque_iniciado = threading.Event()
            permitir_fin = threading.Event()
            systemd_runs: list[str] = []

            def fake_cmd(cmd, timeout=10.0, env=None):
                if cmd[0] == "systemd-run":
                    systemd_runs.append(cmd[2])
                    arranque_iniciado.set()
                    permitir_fin.wait(timeout=3.0)
                    return CommandResult(0, "", "")
                if cmd[:3] == ["systemctl", "--user", "show"]:
                    return CommandResult(
                        0,
                        (
                            f"Id={cmd[3]}\nLoadState=loaded\nActiveState=active\nSubState=running\n"
                            f"ControlGroup={cg_rel}\nMainPID=5000\nPrivateUsers=yes\n"
                            "KillMode=control-group\nInaccessiblePaths=/sys/fs/cgroup\n"
                        ),
                        "",
                    )
                if cmd[:3] == ["systemctl", "--user", "stop"]:
                    cg_abs = os.path.join(cgroup_root, cg_rel.lstrip("/"))
                    with open(os.path.join(cg_abs, "cgroup.procs"), "w", encoding="utf-8") as f:
                        f.write("")
                    with open(os.path.join(cg_abs, "cgroup.events"), "w", encoding="utf-8") as f:
                        f.write("populated 0\n")
                    with open(os.path.join(proc_root, "net", "tcp"), "w", encoding="utf-8") as f:
                        f.write("  sl local_address rem_address st\n")
                    return CommandResult(0, "", "")
                return CommandResult(0, "", "")

            coord = OllamaCoordinator()
            mgr = OllamaServerManager(
                coordinator=coord,
                modo_gestionado=True,
                port=11435,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
                command_runner=fake_cmd,
                nvidia_smi_runner=lambda cmd, timeout=3.0: CommandResult(0, "", ""),
                pidfd_opener=lambda pid, flags: os.dup(0),
                health_probe=lambda _url: True,
                register_atexit=False,
            )
            transport = self._crear_mock_transport(
                chat_lines=[{"model": "qwen2.5:3b", "message": {"content": "ok"}, "done": True}]
            )
            client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:11435")
            prov = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=client)

            with mock.patch("server.services.llm.providers.ollama_lifecycle._ping_pidfd_vivo", return_value=EstadoProceso.MUERTO):
                t1 = asyncio.create_task(prov.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("u1")])))
                self.assertTrue(await asyncio.to_thread(arranque_iniciado.wait, 2.0))
                t2 = asyncio.create_task(prov.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("u2")])))
                await asyncio.sleep(0.02)
                self.assertFalse(t2.done())
                self.assertEqual(len(systemd_runs), 1)

                permitir_fin.set()
                r1, r2 = await asyncio.gather(t1, t2)
                self.assertEqual(r1.texto, "ok")
                self.assertEqual(r2.texto, "ok")
                self.assertEqual(len(systemd_runs), 1)
                await prov.aclose()
            await client.aclose()

    async def test_violacion_gpu_en_prechequeo_detiene_unidad_y_degrada_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            cg_rel = "/user.slice/user-1000.slice/user@1000.service/app.slice/madeline-gpu.service"
            proc_root, cgroup_root = _crear_arbol_proc_cgroup(
                tmp,
                unit_cg=cg_rel,
                main_pid=5000,
                main_starttime=123456,
                port=11435,
            )
            detenido = {"flag": False}
            stops: list[str] = []

            def fake_cmd(cmd, timeout=10.0, env=None):
                if cmd[0] == "systemd-run":
                    return CommandResult(0, "", "")
                if cmd[:3] == ["systemctl", "--user", "show"]:
                    if detenido["flag"]:
                        return CommandResult(0, "LoadState=not-found\nActiveState=inactive\nSubState=dead\n", "")
                    return CommandResult(
                        0,
                        (
                            f"Id={cmd[3]}\nLoadState=loaded\nActiveState=active\nSubState=running\n"
                            f"ControlGroup={cg_rel}\nMainPID=5000\nPrivateUsers=yes\n"
                            "KillMode=control-group\nInaccessiblePaths=/sys/fs/cgroup\n"
                        ),
                        "",
                    )
                if cmd[:3] == ["systemctl", "--user", "stop"]:
                    stops.append(cmd[3])
                    detenido["flag"] = True
                    cg_abs = os.path.join(cgroup_root, cg_rel.lstrip("/"))
                    with open(os.path.join(cg_abs, "cgroup.procs"), "w", encoding="utf-8") as f:
                        f.write("")
                    with open(os.path.join(cg_abs, "cgroup.events"), "w", encoding="utf-8") as f:
                        f.write("populated 0\n")
                    with open(os.path.join(proc_root, "net", "tcp"), "w", encoding="utf-8") as f:
                        f.write("  sl local_address rem_address st\n")
                    return CommandResult(0, "", "")
                return CommandResult(0, "", "")

            coord = OllamaCoordinator()
            mgr = OllamaServerManager(
                coordinator=coord,
                modo_gestionado=True,
                port=11435,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
                command_runner=fake_cmd,
                nvidia_smi_runner=lambda cmd, timeout=3.0: CommandResult(0, "5000, ollama, 512 MiB\n", ""),
                pidfd_opener=lambda pid, flags: os.dup(0),
                health_probe=lambda _url: True,
                register_atexit=False,
            )
            client = httpx.AsyncClient(
                transport=self._crear_mock_transport(chat_lines=[]),
                base_url="http://127.0.0.1:11435",
            )
            prov = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=client)

            with mock.patch("server.services.llm.providers.ollama_lifecycle._ping_pidfd_vivo", return_value=EstadoProceso.MUERTO):
                with self.assertRaises(LLMProviderError) as ctx:
                    await prov.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hola")]))

            self.assertIn("Violación de aislamiento GPU", str(ctx.exception))
            self.assertEqual(mgr.nvidia_telemetria_estado, "VIOLACION_GPU")
            self.assertEqual(len(stops), 1)
            self.assertIsNone(mgr.unit_name)
            self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)
            self.assertFalse(coord.tiene_consumidor_activo())
            self.assertTrue(mgr.revalidar_limpieza_tras_detencion())
            self.assertFalse(await prov.revalidar_limpieza())
            self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)
            await client.aclose()

    async def test_violacion_gpu_en_prechequeo_servidor_externo_degrada_sin_detener(self):
        comandos_externos: list[list[str]] = []
        coord = OllamaCoordinator()
        mgr = OllamaServerManager(
            coordinator=coord,
            modo_gestionado=False,
            port=11434,
            command_runner=lambda cmd, timeout=10.0, env=None: (comandos_externos.append(list(cmd)) or CommandResult(0, "", "")),
            nvidia_smi_runner=lambda cmd, timeout=3.0: CommandResult(0, "7777, ollama, 512 MiB\n", ""),
            register_atexit=False,
        )
        mgr._main_pid = 7777
        client = httpx.AsyncClient(
            transport=self._crear_mock_transport(chat_lines=[]),
            base_url="http://127.0.0.1:11434",
        )
        prov = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=client)

        with self.assertRaises(LLMProviderError) as ctx:
            await prov.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hola")]))
        self.assertIn("Violación de aislamiento GPU", str(ctx.exception))
        self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)
        self.assertEqual(comandos_externos, [])
        self.assertFalse(await prov.recuperar_modo_gestionado())
        await client.aclose()

    async def test_cancelacion_en_acquired_con_fallo_arranque_conserva_no_disponible_y_sin_huerfanos(self):
        arranque_iniciado = threading.Event()
        permitir_fin = threading.Event()

        def failing_cmd(cmd, timeout=10.0, env=None):
            if cmd[0] == "systemd-run":
                arranque_iniciado.set()
                permitir_fin.wait(timeout=3.0)
                return CommandResult(1, "", "boom")
            return CommandResult(0, "", "")

        coord = OllamaCoordinator()
        mgr = OllamaServerManager(
            coordinator=coord,
            modo_gestionado=True,
            port=11435,
            command_runner=failing_cmd,
            register_atexit=False,
        )
        client = httpx.AsyncClient(
            transport=self._crear_mock_transport(chat_lines=[]),
            base_url="http://127.0.0.1:11435",
        )
        prov = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=client)

        t1 = asyncio.create_task(prov.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("u1")])))
        self.assertTrue(await asyncio.to_thread(arranque_iniciado.wait, 2.0))
        t1.cancel()
        await asyncio.sleep(0.02)
        self.assertTrue(coord.tiene_consumidor_activo())
        self.assertFalse(t1.done())

        permitir_fin.set()
        with self.assertRaises(asyncio.CancelledError):
            await t1

        self.assertFalse(coord.tiene_consumidor_activo())
        self.assertEqual(coord.estado_endpoint, EstadoEndpoint.NO_DISPONIBLE)
        self.assertEqual(prov._tareas_arranque, set())
        await client.aclose()

    async def test_revalidar_limpieza_provider_recupera_modo_gestionado_desde_degradado(self):
        with tempfile.TemporaryDirectory() as tmp:
            cg_rel = "/user.slice/user-1000.slice/user@1000.service/app.slice/madeline-rec.service"
            proc_root, cgroup_root = _crear_arbol_proc_cgroup(tmp, unit_cg=cg_rel, main_pid=5000, port=11435)
            detenido = {"flag": False}

            def fake_cmd(cmd, timeout=10.0, env=None):
                if cmd[:3] == ["systemctl", "--user", "show"]:
                    if detenido["flag"]:
                        return CommandResult(0, "LoadState=not-found\nActiveState=inactive\nSubState=dead\n", "")
                    return CommandResult(0, "LoadState=loaded\nActiveState=deactivating\nSubState=stop-sigkill\n", "")
                if cmd[:3] == ["systemctl", "--user", "stop"]:
                    return CommandResult(0, "", "")
                return CommandResult(0, "", "")

            coord = OllamaCoordinator()
            mgr = OllamaServerManager(
                coordinator=coord,
                modo_gestionado=True,
                port=11435,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
                command_runner=fake_cmd,
                pidfd_opener=lambda pid, flags: os.dup(0),
                stop_timeout_s=0.1,
                register_atexit=False,
            )
            mgr._unit_name = "madeline-rec.service"
            mgr._control_group = cg_rel
            mgr._main_pid = 5000
            mgr._inodo_socket_original = 998877
            mgr._ultimo_inodo_socket = 998877
            prov = OllamaProvider(server_manager=mgr, coordinator=coord)

            with mock.patch("server.services.llm.providers.ollama_lifecycle._ping_pidfd_vivo", return_value=EstadoProceso.VIVO):
                self.assertFalse(mgr.detener())
            self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)

            # Aún con cgroup vacío y runner muerto, si el socket original sigue en LISTEN no recupera DISPONIBLE
            detenido["flag"] = True
            cg_abs = os.path.join(cgroup_root, cg_rel.lstrip("/"))
            with open(os.path.join(cg_abs, "cgroup.procs"), "w", encoding="utf-8") as f:
                f.write("")
            with open(os.path.join(cg_abs, "cgroup.events"), "w", encoding="utf-8") as f:
                f.write("populated 0\n")

            with mock.patch("server.services.llm.providers.ollama_lifecycle._ping_pidfd_vivo", return_value=EstadoProceso.MUERTO):
                self.assertFalse(await prov.recuperar_modo_gestionado())
                self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)

                # Ahora cerrar socket residual, pero simular tarea de drenaje pendiente -> sigue bloqueado
                with open(os.path.join(proc_root, "net", "tcp"), "w", encoding="utf-8") as f:
                    f.write("  sl local_address rem_address st\n")
                self.assertTrue(mgr.revalidar_limpieza_tras_detencion())
                self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)

                tarea_pend = asyncio.create_task(asyncio.sleep(0.2))
                prov._registrar_operacion(prov._tareas_drenaje, tarea_pend)
                try:
                    self.assertFalse(await prov.recuperar_modo_gestionado())
                    self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)
                finally:
                    prov._descartar_operacion(prov._tareas_drenaje, tarea_pend)
                    await tarea_pend

                # Con todas las condiciones satisfechas, recuperación explícita restaura DISPONIBLE
                self.assertTrue(await prov.recuperar_modo_gestionado())

            self.assertIsNone(mgr.unit_name)
            self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DISPONIBLE)

    async def test_cancelacion_acquired_unidad_preexistente_propia_detiene_con_cancelaciones_repetidas(self):
        with tempfile.TemporaryDirectory() as tmp:
            cg_rel = "/user.slice/user-1000.slice/user@1000.service/app.slice/madeline-pre.service"
            proc_root, cgroup_root = _crear_arbol_proc_cgroup(
                tmp,
                unit_cg=cg_rel,
                main_pid=5000,
                main_starttime=123456,
                port=11435,
            )
            unit_propia = "madeline-ollama-11435-abcd1234.service"
            verif_en_curso = threading.Event()
            liberar_verif = threading.Event()
            stop_en_curso = threading.Event()
            liberar_stop = threading.Event()
            detenido = {"flag": False}
            stops: list[str] = []

            def fake_cmd(cmd, timeout=10.0, env=None):
                if cmd[:3] == ["systemctl", "--user", "show"]:
                    if detenido["flag"]:
                        return CommandResult(0, "LoadState=not-found\nActiveState=inactive\nSubState=dead\n", "")
                    return CommandResult(
                        0,
                        (
                            f"Id={cmd[3]}\nLoadState=loaded\nActiveState=active\nSubState=running\n"
                            f"ControlGroup={cg_rel}\nMainPID=5000\nPrivateUsers=yes\n"
                            "KillMode=control-group\nInaccessiblePaths=/sys/fs/cgroup\n"
                        ),
                        "",
                    )
                if cmd[:3] == ["systemctl", "--user", "stop"]:
                    stops.append(cmd[3])
                    stop_en_curso.set()
                    liberar_stop.wait(timeout=3.0)
                    detenido["flag"] = True
                    cg_abs = os.path.join(cgroup_root, cg_rel.lstrip("/"))
                    with open(os.path.join(cg_abs, "cgroup.procs"), "w", encoding="utf-8") as f:
                        f.write("")
                    with open(os.path.join(cg_abs, "cgroup.events"), "w", encoding="utf-8") as f:
                        f.write("populated 0\n")
                    with open(os.path.join(proc_root, "net", "tcp"), "w", encoding="utf-8") as f:
                        f.write("  sl local_address rem_address st\n")
                    return CommandResult(0, "", "")
                return CommandResult(0, "", "")

            coord = OllamaCoordinator()
            mgr = OllamaServerManager(
                coordinator=coord,
                modo_gestionado=True,
                port=11435,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
                command_runner=fake_cmd,
                pidfd_opener=lambda pid, flags: os.dup(0),
                register_atexit=False,
            )
            mgr._unit_name = unit_propia
            mgr._control_group = cg_rel
            mgr._main_pid = 5000
            mgr._main_starttime = 123456
            mgr._inodo_socket_original = 998877
            mgr._ultimo_inodo_socket = 998877
            mgr._aislamiento_verificado = True

            def verif_fallida(_unit: str):
                verif_en_curso.set()
                liberar_verif.wait(timeout=3.0)
                raise LLMProviderError("mountinfo alterado", provider="ollama")

            client = httpx.AsyncClient(
                transport=self._crear_mock_transport(chat_lines=[]),
                base_url="http://127.0.0.1:11435",
            )
            prov = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=client)

            with mock.patch.object(mgr, "_verificar_unidad_y_aislamiento", side_effect=verif_fallida):
                with mock.patch("server.services.llm.providers.ollama_lifecycle._ping_pidfd_vivo", return_value=EstadoProceso.MUERTO):
                    t1 = asyncio.create_task(prov.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hola")])))
                    self.assertTrue(await asyncio.to_thread(verif_en_curso.wait, 2.0))
                    t1.cancel()
                    await asyncio.sleep(0.02)
                    liberar_verif.set()

                    self.assertTrue(await asyncio.to_thread(stop_en_curso.wait, 2.0))
                    # Cancelación repetida durante limpieza no puede liberar el slot antes de concluir
                    t1.cancel()
                    await asyncio.sleep(0.02)
                    self.assertTrue(coord.tiene_consumidor_activo())
                    self.assertFalse(t1.done())

                    liberar_stop.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await t1

            self.assertEqual(stops, [unit_propia])
            self.assertIsNone(mgr.unit_name)
            self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)
            self.assertFalse(coord.tiene_consumidor_activo())
            await client.aclose()

    async def test_cancelacion_acquired_unidad_no_verificable_o_externa_no_detiene_especulativamente(self):
        with tempfile.TemporaryDirectory() as tmp:
            cg_rel = "/user.slice/user-1000.slice/user@1000.service/app.slice/ajena.service"
            proc_root, cgroup_root = _crear_arbol_proc_cgroup(
                tmp,
                unit_cg=cg_rel,
                main_pid=5000,
                main_starttime=123456,
                port=11435,
            )
            stops: list[str] = []
            verif_en_curso = threading.Event()
            liberar_verif = threading.Event()

            def fake_cmd(cmd, timeout=10.0, env=None):
                if cmd[:3] == ["systemctl", "--user", "stop"]:
                    stops.append(cmd[3])
                return CommandResult(0, "", "")

            # Caso 1: Unidad managed cuyo nombre/identidad no coincide con Madeline -> NO detener
            coord = OllamaCoordinator()
            mgr = OllamaServerManager(
                coordinator=coord,
                modo_gestionado=True,
                port=11435,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
                command_runner=fake_cmd,
                register_atexit=False,
            )
            mgr._unit_name = "unidad-ajena-externa.service"
            mgr._control_group = cg_rel
            mgr._main_pid = 5000
            mgr._main_starttime = 123456
            mgr._aislamiento_verificado = True

            def verif_falla(_u: str):
                verif_en_curso.set()
                liberar_verif.wait(timeout=3.0)
                raise LLMProviderError("pérdida verificación", provider="ollama")

            client = httpx.AsyncClient(
                transport=self._crear_mock_transport(chat_lines=[]),
                base_url="http://127.0.0.1:11435",
            )
            prov = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=client)
            with mock.patch.object(mgr, "_verificar_unidad_y_aislamiento", side_effect=verif_falla):
                t1 = asyncio.create_task(prov.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("u")])))
                self.assertTrue(await asyncio.to_thread(verif_en_curso.wait, 2.0))
                t1.cancel()
                liberar_verif.set()
                with self.assertRaises(asyncio.CancelledError):
                    await t1

            self.assertEqual(stops, [])
            self.assertEqual(coord.estado_endpoint, EstadoEndpoint.DEGRADADO)
            self.assertIn("se omite detención especulativa", coord.motivo_estado)
            await client.aclose()

            # Caso 2: Modo externo nunca ejecuta detención
            coord_ext = OllamaCoordinator()
            mgr_ext = OllamaServerManager(
                coordinator=coord_ext,
                modo_gestionado=False,
                port=11434,
                command_runner=fake_cmd,
                register_atexit=False,
            )
            propia, _ = mgr_ext.verificar_propiedad_unidad_activa()
            self.assertFalse(propia)
            self.assertEqual(stops, [])

    async def test_keep_alive_validacion_estricta_en_provider_y_manager(self):
        invalidos = (
            "0",
            "0s",
            "0.0s",
            "0m0s",
            "0h0m0.0s",
            "0.000000000000000000001ns",
            "0.5ns",
            "0.0001us",
            "٥m",
            "5٠",
            "999999999999999999999h",
            "9223372036854775808ns",
            "9223372037",
            ".0s",
            ".5s",
            "5.",
            "-5m",
            "-1",
            "-0s",
            "+5m",
            " 5m",
            "5m ",
            "5 m",
            "",
            "   ",
            "5d",
            "abc",
            0,
            0.0,
            1e-10,
            10_000_000_000,
            -1,
            True,
            False,
            None,
            float("nan"),
            float("inf"),
        )
        peticiones_http: list[str] = []

        async def _fail_if_http_called(request: httpx.Request) -> httpx.Response:
            peticiones_http.append(request.url.path)
            return httpx.Response(500)

        client_guard = httpx.AsyncClient(
            transport=httpx.MockTransport(_fail_if_http_called),
            base_url="http://127.0.0.1:11435",
        )
        try:
            for valor in invalidos:
                with self.assertRaises(LLMConfigurationError, msg=f"OllamaProvider aceptó keep_alive={valor!r}"):
                    OllamaProvider(keep_alive=valor, http_client=client_guard)
                with self.assertRaises(LLMConfigurationError, msg=f"OllamaServerManager aceptó keep_alive={valor!r}"):
                    OllamaServerManager(keep_alive=valor, register_atexit=False)
            self.assertEqual(peticiones_http, [])
        finally:
            await client_guard.aclose()

        validos = ("5m", "1h30m", "250ms", "1.5s", "1ns", "9223372036854775807ns", "300", 300, 0.5)
        for valor in validos:
            mgr_ok = OllamaServerManager(keep_alive=valor, register_atexit=False)
            prov_ok = OllamaProvider(keep_alive=valor, server_manager=mgr_ok)
            self.assertEqual(mgr_ok._keep_alive, valor)
            self.assertEqual(prov_ok._keep_alive, valor)

    async def test_descargar_modelo_bloquea_nueva_inferencia_concurrente(self):
        coord = OllamaCoordinator()
        mgr = mock.MagicMock(spec=OllamaServerManager)
        mgr.coordinator = coord
        mgr.base_url = "http://127.0.0.1:11435"
        mgr.modo_gestionado = True

        descarga_iniciada = asyncio.Event()
        liberar_descarga = asyncio.Event()
        eventos: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/generate":
                eventos.append("generate_start")
                descarga_iniciada.set()
                await liberar_descarga.wait()
                eventos.append("generate_end")
                return httpx.Response(200, json={"done": True})
            if request.url.path == "/api/chat":
                eventos.append("chat_start")
                body = json.dumps({"model": "qwen2.5:3b", "message": {"content": "ok"}, "done": True}) + "\n"
                return httpx.Response(200, content=body.encode("utf-8"))
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": [{"model": "qwen2.5:3b", "size_vram": 0}]})
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11435")
        prov = OllamaProvider(server_manager=mgr, coordinator=coord, http_client=client)

        t_desc = asyncio.create_task(prov.descargar_modelo())
        await descarga_iniciada.wait()

        # Lanzar inferencia mientras descarga está activa: no puede iniciar hasta que descarga termine
        t_chat = asyncio.create_task(prov.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hola")])))
        await asyncio.sleep(0.05)
        self.assertEqual(eventos, ["generate_start"])

        liberar_descarga.set()
        self.assertTrue(await t_desc)
        resp = await t_chat
        self.assertEqual(resp.texto, "ok")
        self.assertEqual(eventos, ["generate_start", "generate_end", "chat_start"])
        self.assertFalse(coord.tiene_consumidor_activo())
        await client.aclose()

    async def test_tool_choice_no_soportado_y_keep_alive_cero_rechazados(self):
        with self.assertRaises(LLMConfigurationError):
            OllamaProvider(keep_alive="0")
        with self.assertRaises(LLMConfigurationError):
            OllamaProvider(keep_alive="0s")

        mgr = mock.MagicMock(spec=OllamaServerManager)
        mgr.coordinator = OllamaCoordinator()
        mgr.base_url = "http://127.0.0.1:11435"
        mgr.modo_gestionado = True
        prov = OllamaProvider(server_manager=mgr)

        tools = [LLMToolDefinition(nombre="abrir", descripcion="Abre app", parametros={"type": "object"})]
        for modo in (LLMToolChoice.required(), LLMToolChoice.specific("abrir")):
            req = LLMRequest(mensajes=[LLMMessage.user("abre")], tools=tools, tool_choice=modo)
            with self.assertRaises(LLMValidationError):
                await prov.generar_respuesta(req)

    async def test_cpu_only_verificado_requiere_telemetria_nvidia_confirmada(self):
        transport = self._crear_mock_transport(
            chat_lines=[{"model": "qwen2.5:3b", "message": {"content": "ok"}, "done": True}]
        )
        client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:11435")

        # Caso 1: nvidia-smi ausente -> DESCONOCIDA -> cpu_only_verificado es False
        coord1 = OllamaCoordinator()
        mgr_desc = OllamaServerManager(
            coordinator=coord1,
            modo_gestionado=False,
            port=11435,
            nvidia_smi_runner=lambda cmd, timeout=3.0: (_ for _ in ()).throw(FileNotFoundError("no")),
            register_atexit=False,
        )
        p1 = OllamaProvider(server_manager=mgr_desc, coordinator=coord1, http_client=client)
        await p1.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hola")]))
        self.assertEqual(p1.ultima_telemetria_gpu, "DESCONOCIDA")
        self.assertFalse(p1.cpu_only_verificado)

        # Caso 2: nvidia-smi sin procesos propios + /api/ps size_vram=0 -> cpu_only_verificado True
        coord2 = OllamaCoordinator()
        mgr_ok = OllamaServerManager(
            coordinator=coord2,
            modo_gestionado=False,
            port=11435,
            nvidia_smi_runner=lambda cmd, timeout=3.0: CommandResult(0, "", ""),
            register_atexit=False,
        )
        p2 = OllamaProvider(server_manager=mgr_ok, coordinator=coord2, http_client=client)
        await p2.generar_respuesta(LLMRequest(mensajes=[LLMMessage.user("hola")]))
        self.assertEqual(p2.ultima_telemetria_gpu, "SIN_PROCESOS_GPU")
        self.assertTrue(p2.cpu_only_verificado)
        await client.aclose()


class TestLLMFactory(unittest.TestCase):
    """Pruebas de selección explícita de proveedor manteniendo Groq por defecto."""

    def test_factory_predeterminado_es_groq(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MADELINE_LLM_PROVIDER", None)
            prov = crear_proveedor_llm(groq_api_key="gsk_test")
            self.assertIsInstance(prov, GroqProvider)
            self.assertEqual(prov.nombre, "groq")

    def test_factory_ollama_explicito_por_env_y_parametro(self):
        with mock.patch.dict(os.environ, {"MADELINE_LLM_PROVIDER": "ollama"}):
            prov = crear_proveedor_llm(resource_manager=ResourceManager(fallback_habilitado=True))
            self.assertIsInstance(prov, OllamaProvider)
            self.assertEqual(prov.nombre, "ollama")
            self.assertEqual(prov.base_url, "http://127.0.0.1:11435")

    def test_factory_ollama_resuelve_resource_manager_por_defecto(self):
        prov = crear_proveedor_llm("ollama")
        self.assertIsInstance(prov, OllamaProvider)
        self.assertIsInstance(prov._resource_manager, ResourceManager)

    def test_factory_proveedor_desconocido_lanza_configuration_error(self):
        with self.assertRaises(LLMConfigurationError):
            crear_proveedor_llm("desconocido")

    def test_importacion_relativa_desde_server_en_sys_path(self):
        import subprocess
        import sys

        res = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, 'server'); "
                "from services.llm import OllamaProvider, OllamaCoordinator, OllamaServerManager, crear_proveedor_llm; "
                "assert crear_proveedor_llm(groq_api_key='x').nombre == 'groq'",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == "__main__":
    unittest.main()
