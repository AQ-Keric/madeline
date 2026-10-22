// Avatar de Madeline: una monita pixel art que vive sobre tus ventanas.
//
// Lo arranca y lo cierra el servidor (mostrar_avatar en
// madeline_server.py) con:  qs -p ~/asistente_local/avatar
//
// Como en Celeste, usa la pantalla de nivel: el borde de abajo es el
// piso y el borde de arriba de cada ventana (si cabe bajo la barra) es
// una plataforma. Pasea, se tira para abajo de las ventanas y de vez en
// cuando hace dash, también para subirse a una (el pelo se pone azul
// hasta que aterriza y deja una estela). Dónde están las ventanas se lee con
// hyprctl solo cuando Hyprland avisa que algo cambió.
//
// El estado de Madeline lo escribe el servidor en
// $XDG_RUNTIME_DIR/madeline_estado (quieta | escuchando | pensando |
// hablando). La ventana del avatar no recibe clics (mask vacía) y se
// esconde si hay algo en pantalla completa (juegos, videos).
//
// La luz de los audífonos es un hoyo en el sprite (sprites/luz.json
// dice dónde) y se pinta por debajo: en reposo tiene su color, al
// escuchar, pensar o hablar pasa por el arcoíris, y con música sonando
// (cualquier reproductor MPRIS) parpadea cambiando de color.

import QtQuick
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io
import Quickshell.Services.Mpris
import Quickshell.Wayland

ShellRoot {
    PanelWindow {
        id: ventana

        // Tamaño del sprite en pantalla: 32 px x escala.
        readonly property int escala: 3
        readonly property int lado: 32 * escala
        readonly property real velocidad: 45     // caminando, px/s

        // Lo que está haciendo Madeline (lo manda el servidor).
        property string estado: "quieta"
        // Lo que hace el avatar por su cuenta: quieta, caminar, dormir,
        // saltar, caer o dash.
        property string modo: "quieta"
        property bool azul: false        // pelo azul: después del dash
        property bool haciaLaDerecha: true
        property int cuadro: 0
        property bool luzApagada: false
        property real tono: 0.75
        property var luz: ({ reposo: "#aa78ff", apagada: "#333344", cuadros: {} })

        // La escena: {x0, x1, y} donde se puede parar.
        property var superficies: [{ x0: 0, x1: width, y: height, suelo: true }]
        property real techo: 0
        property bool pantallaCompleta: false

        // Mpris avisa solo cuando cambia algo: no hay que consultar.
        readonly property bool hayMusica: Mpris.players.values.some(p => p.isPlaying)
        readonly property bool atendiendo: estado !== "quieta"
        readonly property bool apoyada: modo === "quieta" || modo === "caminar" || modo === "dormir"

        readonly property string animacion: {
            if (apoyada) {
                if (estado === "hablando") return "hablar";
                if (estado === "pensando") return "pensar";
                if (estado === "escuchando") return "escuchar";
                return modo;
            }
            if (modo === "caer" && azul) return "caer_azul";
            return modo;
        }
        readonly property var cuadrosPorAnimacion: ({
            caminar: 4, hablar: 2, pensar: 2, escuchar: 2, dormir: 2
        })

        readonly property color colorLuz: {
            if (luzApagada) return luz.apagada;
            if (atendiendo || hayMusica) return Qt.hsla(tono, 0.95, 0.62, 1);
            if (modo === "dormir") return Qt.darker(luz.reposo, 1.8);
            return luz.reposo;
        }

        screen: Quickshell.screens[0]
        anchors { left: true; right: true; top: true; bottom: true }
        exclusionMode: ExclusionMode.Ignore
        color: "transparent"
        visible: !pantallaCompleta
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.namespace: "madeline-avatar"
        WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
        mask: Region {}

        // ------------------------------------------------------------
        // Estado de Madeline y luz de los audífonos
        // ------------------------------------------------------------

        FileView {
            path: Quickshell.env("XDG_RUNTIME_DIR") + "/madeline_estado"
            watchChanges: true
            printErrors: false
            onFileChanged: reload()
            onLoaded: ventana.estado = text().trim() || "quieta"
            onLoadFailed: ventana.estado = "quieta"
        }

        FileView {
            path: Qt.resolvedUrl("sprites/luz.json")
            onLoaded: ventana.luz = JSON.parse(text())
        }

        // Si una grabación queda vacía el servidor nunca avisa que terminó:
        // a los 30 s sin cambios vuelve a lo suyo.
        Timer {
            id: olvidar
            interval: 30000
            onTriggered: if (ventana.estado !== "hablando") ventana.estado = "quieta"
        }

        onEstadoChanged: {
            olvidar.restart();
            if (estado === "quieta") {
                programar(2500);
                return;
            }
            // Se detiene a atender (si va en el aire, termina de caer).
            decidir.stop();
            if (modo === "caminar" || modo === "dormir") {
                mov.stop();
                modo = "quieta";
            }
        }

        onAnimacionChanged: cuadro = 0

        // Arcoíris mientras escucha, piensa o habla.
        Timer {
            interval: 90
            running: ventana.atendiendo
            repeat: true
            onTriggered: ventana.tono = (ventana.tono + 0.04) % 1
        }

        // Con música: parpadea y cada vez que se prende cambia de color.
        Timer {
            interval: 420
            running: ventana.hayMusica && !ventana.atendiendo
            repeat: true
            onTriggered: {
                ventana.luzApagada = !ventana.luzApagada;
                if (!ventana.luzApagada)
                    ventana.tono = (ventana.tono + 0.19) % 1;
            }
            onRunningChanged: ventana.luzApagada = false
        }

        // ------------------------------------------------------------
        // La escena: ventanas del escritorio activo
        // ------------------------------------------------------------

        readonly property var eventosEscena: [
            "openwindow", "closewindow", "movewindowv2", "workspacev2",
            "changefloatingmode", "fullscreen", "activewindowv2", "focusedmonv2"
        ]

        Connections {
            target: Hyprland
            function onRawEvent(evento) {
                if (ventana.eventosEscena.includes(evento.name))
                    refrescar.restart();
            }
        }

        // Espera a que terminen las animaciones de Hyprland antes de leer.
        Timer {
            id: refrescar
            interval: 450
            onTriggered: leerEscena.running = true
        }

        Process {
            id: leerEscena
            command: ["sh", "-c", "hyprctl -j monitors; echo @@; hyprctl -j clients"]
            stdout: StdioCollector {
                onStreamFinished: ventana.armarEscena(text)
            }
        }

        Component.onCompleted: refrescar.restart()

        function armarEscena(texto) {
            const partes = texto.split("@@");
            let monitores, clientes;
            try {
                monitores = JSON.parse(partes[0]);
                clientes = JSON.parse(partes[1]);
            } catch (e) {
                return;
            }
            const mon = monitores.find(m => m.name === ventana.screen.name) || monitores[0];
            if (!mon)
                return;
            const propias = clientes.filter(c => c.workspace.id === mon.activeWorkspace.id
                                             && c.mapped && !c.hidden);
            techo = mon.reserved[1];
            pantallaCompleta = propias.some(c => c.fullscreen === true || c.fullscreen >= 2);

            const sups = [{ x0: 0, x1: width, y: height, suelo: true }];
            for (const c of propias) {
                const y = c.at[1] - mon.y;
                if (y - lado >= techo + 4)
                    sups.push({ x0: c.at[0] - mon.x, x1: c.at[0] - mon.x + c.size[0], y: y, suelo: false });
            }
            superficies = sups;
            // Si la ventana en la que estaba parada se fue, se cae.
            if (!ocupada() && !superficieEn(piesX(), piesY()))
                caer();
        }

        // ------------------------------------------------------------
        // Movimiento
        // ------------------------------------------------------------

        function piesX() { return monita.x + lado / 2; }
        function piesY() { return monita.y + lado; }
        function limitarX(x) { return Math.max(0, Math.min(width - lado, x)); }
        function azar(min, max) { return min + Math.random() * (max - min); }

        function superficieEn(x, y) {
            return superficies.find(s => Math.abs(s.y - y) <= 3 && x >= s.x0 && x <= s.x1);
        }

        // La primera superficie bajo (x, y); el piso siempre está.
        function superficieDebajo(x, y) {
            let mejor = null;
            for (const s of superficies)
                if (s.y >= y - 3 && x >= s.x0 && x <= s.x1 && (!mejor || s.y < mejor.y))
                    mejor = s;
            return mejor || superficies[0];
        }

        // Dónde puede quedar el sprite (su x) con los pies sobre s.
        function xEn(s) {
            const min = limitarX(s.x0 - lado / 2 + 12), max = limitarX(s.x1 - lado / 2 - 12);
            return azar(min, Math.max(min, max));
        }

        function ocupada() {
            return mov.running || !apoyada;
        }

        function programar(ms) {
            decidir.interval = ms;
            decidir.restart();
        }

        function descansar(ms) {
            modo = "quieta";
            programar(ms);
        }

        // Movimiento a saltos de un píxel del sprite (escala px), como un
        // juego pixel art, y a pocos cuadros por segundo: la ventana
        // cubre toda la pantalla y cada cuadro se redibuja entera (a 60
        // fps caminando gastaba ~4 % de CPU).
        Timer {
            id: mov
            property real x0
            property real y0
            property real x1
            property real y1
            property real inicio
            property real duracion
            property string curvaX
            property string curvaY
            property var alTerminar: null
            repeat: true
            onTriggered: {
                const p = Math.min(1, (Date.now() - inicio) / duracion);
                const e = ventana.escala;
                monita.x = Math.round((x0 + (x1 - x0) * ventana.curva(curvaX, p)) / e) * e;
                monita.y = Math.round((y0 + (y1 - y0) * ventana.curva(curvaY, p)) / e) * e;
                if (p >= 1) {
                    stop();
                    const fin = alTerminar;
                    alTerminar = null;
                    if (fin)
                        Qt.callLater(fin);
                }
            }
        }

        function curva(tipo, p) {
            if (tipo === "sale") return 1 - (1 - p) * (1 - p);   // frena al final
            if (tipo === "entra") return p * p;                   // acelera (caída)
            return p;
        }

        // fps: 15 caminando (un paso de 3 px por cuadro), 30 en el aire.
        function mover(x, y, ms, curvaX, curvaY, fin, fps) {
            mov.stop();
            mov.x0 = monita.x;
            mov.y0 = monita.y;
            mov.x1 = x;
            mov.y1 = y;
            mov.duracion = Math.max(1, ms);
            mov.curvaX = curvaX;
            mov.curvaY = curvaY;
            mov.alTerminar = fin || null;
            mov.interval = 1000 / (fps || 30);
            mov.inicio = Date.now();
            mov.start();
        }

        function esperar(ms, fin) {
            mover(monita.x, monita.y, ms, "", "", fin, 4);
        }

        function caminarA(x, fin) {
            x = limitarX(x);
            const dist = Math.abs(x - monita.x);
            if (dist < 4) {
                fin ? fin() : descansar(800);
                return;
            }
            haciaLaDerecha = x > monita.x;
            modo = "caminar";
            mover(x, monita.y, dist / velocidad * 1000, "lineal", "lineal",
                  fin || (() => descansar(azar(1500, 4500))), velocidad / escala);
        }

        function caer() {
            const s = superficieDebajo(piesX(), piesY());
            const dist = s.y - piesY();
            if (dist <= 2) {
                aterrizar();
                return;
            }
            modo = "caer";
            mover(monita.x, s.y - lado, 90 + Math.sqrt(dist) * 24, "lineal", "entra", aterrizar);
        }

        function aterrizar() {
            azul = false;
            descansar(estado === "quieta" ? azar(900, 2400) : 2500);
        }

        // Dash: un impulso corto, sale disparada hasta quedar un poco
        // sobre el destino y cae. El pelo queda azul hasta aterrizar.
        function dashA(x, s) {
            x = limitarX(x);
            const yFin = Math.max(techo, s.y - lado - 30);
            if (Math.abs(x - monita.x) > 2)
                haciaLaDerecha = x > monita.x;
            modo = "saltar";
            mover(monita.x, monita.y - 6, 110, "lineal", "sale", () => {
                modo = "dash";
                azul = true;
                const dist = Math.hypot(x - monita.x, yFin - monita.y);
                mover(x, yFin, Math.max(140, Math.min(480, dist / 1.6)), "sale", "sale", caer);
            });
        }

        // Bajarse de una ventana: camina a un borde libre y salta afuera.
        function bajar(s) {
            const bordes = [];
            if (s.x1 + 30 < width - lado / 2)
                bordes.push({ x: s.x1 - lado / 2, dir: 1 });
            if (s.x0 - 30 > lado / 2)
                bordes.push({ x: s.x0 - lado / 2, dir: -1 });
            if (!bordes.length) {
                const piso = superficies[0];
                dashA(xEn(piso), piso);
                return;
            }
            const b = bordes[Math.floor(Math.random() * bordes.length)];
            caminarA(b.x, () => {
                haciaLaDerecha = b.dir === 1;
                modo = "saltar";
                mover(limitarX(monita.x + b.dir * 70), monita.y - 26, 220, "lineal", "sale", caer);
            });
        }

        // Elige qué hacer cuando termina lo anterior.
        Timer {
            id: decidir
            interval: 1500
            running: true
            onTriggered: {
                if (ventana.estado !== "quieta" || ventana.ocupada())
                    return;
                const s = ventana.superficieEn(ventana.piesX(), ventana.piesY());
                if (!s) {
                    ventana.caer();
                    return;
                }
                const opciones = [["caminar", 40], ["quieta", 18], ["dormir", 6], ["dash", 16]];
                if (!s.suelo)
                    opciones.push(["bajar", 16]);
                let total = opciones.reduce((a, o) => a + o[1], 0);
                let r = Math.random() * total;
                const elegida = opciones.find(o => (r -= o[1]) < 0)[0];

                switch (elegida) {
                case "caminar":
                    ventana.caminarA(ventana.xEn(s));
                    break;
                case "quieta":
                    ventana.descansar(ventana.azar(3000, 8000));
                    break;
                case "dormir":
                    ventana.modo = "dormir";
                    ventana.programar(ventana.azar(30000, 120000));
                    break;
                case "bajar":
                    ventana.bajar(s);
                    break;
                case "dash": {
                    const otras = ventana.superficies.filter(o => o !== s && o.x1 - o.x0 > ventana.lado);
                    const destino = otras.length && Math.random() < 0.7
                        ? otras[Math.floor(Math.random() * otras.length)] : s;
                    let x = ventana.xEn(destino);
                    if (destino === s && Math.abs(x - monita.x) < 160)
                        x = monita.x + (x < monita.x ? -1 : 1) * ventana.azar(200, 450);
                    ventana.dashA(x, destino);
                    break;
                }
                }
            }
        }

        // Cambia de cuadro. Quieta pestañea de vez en cuando (cuadro 2).
        Timer {
            interval: {
                switch (ventana.animacion) {
                case "caminar": return 140;
                case "hablar": return 170;
                case "dormir": return 900;
                default: return 450;
                }
            }
            running: ventana.visible
            repeat: true
            onTriggered: {
                if (ventana.animacion === "quieta") {
                    const secuencia = [0, 1, 0, 1, 0, 1, 0, 2];
                    ventana.cuadro = secuencia[(secuencia.indexOf(ventana.cuadro) + 1 + Math.floor(Math.random() * 2)) % secuencia.length];
                } else {
                    const total = ventana.cuadrosPorAnimacion[ventana.animacion] || 1;
                    ventana.cuadro = (ventana.cuadro + 1) % total;
                }
            }
        }

        // ------------------------------------------------------------
        // Dibujo
        // ------------------------------------------------------------

        // Estela del dash: siluetas celestes que se desvanecen.
        Repeater {
            id: estela
            model: 6
            delegate: Item {
                id: fantasma
                width: ventana.lado
                height: ventana.lado
                opacity: 0
                visible: opacity > 0
                property bool espejo: false

                function soltar(x, y, derecha) {
                    fantasma.x = x;
                    fantasma.y = y;
                    espejo = !derecha;
                    desvanecer.restart();
                }

                Image {
                    anchors.fill: parent
                    source: "sprites/dash_silueta.png"
                    smooth: false
                    transform: Scale {
                        origin.x: ventana.lado / 2
                        xScale: fantasma.espejo ? -1 : 1
                    }
                }

                NumberAnimation on opacity {
                    id: desvanecer
                    running: false
                    from: 0.85
                    to: 0
                    duration: 340
                    easing.type: Easing.OutQuad
                }
            }
        }

        Timer {
            property int siguiente: 0
            interval: 40
            running: ventana.modo === "dash"
            repeat: true
            onTriggered: estela.itemAt(siguiente++ % estela.count).soltar(monita.x, monita.y, ventana.haciaLaDerecha)
        }

        Item {
            id: monita
            width: ventana.lado
            height: ventana.lado
            x: ventana.width / 3
            y: ventana.height - height

            Item {
                anchors.fill: parent
                transform: Scale {
                    origin.x: ventana.lado / 2
                    xScale: ventana.haciaLaDerecha ? 1 : -1
                }

                readonly property string nombre: ventana.animacion + "_" + ventana.cuadro
                readonly property var caja: ventana.luz.cuadros[nombre] || [0, 0, 0, 0]

                // Va debajo del sprite y se ve por el hoyo de la luz.
                Rectangle {
                    x: parent.caja[0] * ventana.escala
                    y: parent.caja[1] * ventana.escala
                    width: parent.caja[2] * ventana.escala
                    height: parent.caja[3] * ventana.escala
                    color: ventana.colorLuz
                }

                Image {
                    anchors.fill: parent
                    source: "sprites/" + parent.nombre + ".png"
                    smooth: false
                    mipmap: false
                }
            }

            // Globito sobre la cabeza: ! escuchando, ... pensando,
            // ♪ hablando y zZ durmiendo.
            Text {
                anchors.bottom: parent.top
                anchors.bottomMargin: -10
                x: ventana.haciaLaDerecha ? parent.width * 0.55 : parent.width * 0.2
                font.family: "monospace"
                font.pixelSize: 22
                font.bold: true
                color: ventana.animacion === "dormir" ? "#b98af0" : ventana.colorLuz
                style: Text.Outline
                styleColor: "#1a1423"
                text: {
                    switch (ventana.animacion) {
                    case "escuchar": return "!";
                    case "pensar": return ".".repeat(ventana.cuadro + 2);
                    case "hablar": return "♪";
                    case "dormir": return ventana.cuadro ? "zZ" : "z";
                    default: return "";
                    }
                }
            }
        }
    }
}
