import 'dart:async';
import 'dart:convert';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:gal/gal.dart';
import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:record/record.dart';

import 'conexion.dart';
import 'pantalla_deck.dart';
import 'tema.dart';
import 'widgets/burbuja.dart';
import 'widgets/fondo_montana.dart';
import 'widgets/panel_sensores.dart';

class PantallaPrincipal extends StatefulWidget {
  const PantallaPrincipal({super.key});

  @override
  State<PantallaPrincipal> createState() => _PantallaPrincipalState();
}

class _PantallaPrincipalState extends State<PantallaPrincipal> with WidgetsBindingObserver {
  final _conexion = Conexion();
  final _grabadora = AudioRecorder();
  final _reproductor = AudioPlayer();
  final _texto = TextEditingController();
  final _scroll = ScrollController();
  final List<Mensaje> _mensajes = [
    Mensaje('Hola, Keric. Mantén presionado el micrófono para hablarme, o escríbeme.', deMadeline: true),
  ];

  // 0: chat, 1: deck de botones.
  int _pestana = 0;

  bool _grabando = false;
  bool _pensando = false;
  bool _hayTexto = false;
  // Si el dedo sigue sobre el botón. La primera vez, el diálogo de
  // permiso del micrófono hace que se suelte ANTES de que arranque la
  // grabación; sin esto quedaba grabando sin fin.
  bool _dedoAbajo = false;

  // Capturas pedidas desde el PC: se revisan al abrir la app, al volver
  // a ella y cada pocos segundos mientras está abierta.
  Timer? _timerBandeja;
  bool _revisandoBandeja = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _timerBandeja = Timer.periodic(const Duration(seconds: 8), (_) => _revisarBandeja());
    _texto.addListener(() {
      final hay = _texto.text.trim().isNotEmpty;
      if (hay != _hayTexto) setState(() => _hayTexto = hay);
    });
    _conectar();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState estado) {
    if (estado == AppLifecycleState.resumed) _revisarBandeja();
  }

  Future<void> _revisarBandeja() async {
    if (_revisandoBandeja || _conexion.estado != EstadoConexion.conectada) return;
    _revisandoBandeja = true;
    try {
      for (final id in await _conexion.bandeja()) {
        final mensaje = Mensaje('📸 La captura que pediste desde el PC', deMadeline: true);
        await _bajarCaptura('/bandeja/$id', mensaje, alLlegar: () => _agregar(mensaje));
      }
    } finally {
      _revisandoBandeja = false;
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _timerBandeja?.cancel();
    _grabadora.dispose();
    _reproductor.dispose();
    _texto.dispose();
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _conectar({bool forzar = false}) async {
    setState(() => _conexion.estado = EstadoConexion.buscando);
    await _conexion.conectar(forzarBusqueda: forzar);
    if (mounted) setState(() {});
    _revisarBandeja();
  }

  void _agregar(Mensaje m) {
    setState(() => _mensajes.add(m));
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.animateTo(_scroll.position.maxScrollExtent + 200,
            duration: const Duration(milliseconds: 300), curve: Curves.easeOut);
      }
    });
  }

  // ------------------------------------------------------------
  // Hablar
  // ------------------------------------------------------------

  Future<void> _empezarGrabacion() async {
    if (!await Permission.microphone.request().isGranted) {
      _avisar('Necesito permiso de micrófono para escucharte.');
      return;
    }
    final ruta = '${(await getTemporaryDirectory()).path}/madeline_orden.wav';
    await _grabadora.start(const RecordConfig(encoder: AudioEncoder.wav, sampleRate: 16000, numChannels: 1),
        path: ruta);

    if (!_dedoAbajo) {
      await _grabadora.stop();
      return;
    }
    HapticFeedback.mediumImpact();
    await _reproductor.stop();
    setState(() => _grabando = true);
  }

  Future<void> _terminarGrabacion() async {
    if (!_grabando) return;
    final ruta = await _grabadora.stop();
    HapticFeedback.lightImpact();
    setState(() => _grabando = false);
    if (ruta == null) return;

    final burbuja = Mensaje('🎙️ …', deMadeline: false);
    _agregar(burbuja);
    await _enviar(() => _conexion.enviarAudio(ruta), burbujaVoz: burbuja);
  }

  Future<void> _enviarTexto() async {
    final texto = _texto.text.trim();
    if (texto.isEmpty) return;
    _texto.clear();
    _agregar(Mensaje(texto, deMadeline: false));
    await _enviar(() => _conexion.enviarTexto(texto));
  }

  Future<void> _enviar(Future<http.Response> Function() peticion, {Mensaje? burbujaVoz}) async {
    setState(() => _pensando = true);
    try {
      final respuesta = await peticion();
      await _procesarRespuesta(respuesta, burbujaVoz: burbujaVoz);
    } catch (_) {
      _agregar(Mensaje(
        'No encuentro tu PC en esta red. Revisa que el servidor esté encendido y que estés en el mismo WiFi.',
        deMadeline: true,
        esError: true,
      ));
    } finally {
      if (mounted) setState(() => _pensando = false);
    }
  }

  Future<void> _procesarRespuesta(http.Response r, {Mensaje? burbujaVoz}) async {
    Map<String, dynamic> datos = {};
    try {
      datos = jsonDecode(utf8.decode(r.bodyBytes)) as Map<String, dynamic>;
    } catch (_) {}

    // Muestra lo que entendió Whisper: si se equivocó, se ve al tiro.
    if (burbujaVoz != null) {
      final oido = datos['transcripcion']?.toString();
      setState(() => burbujaVoz.texto = oido == null || oido.isEmpty ? '🎙️ (no se entendió)' : '🎙️ $oido');
    }

    if (r.statusCode != 200) {
      _agregar(Mensaje(datos['detail']?.toString() ?? 'El servidor respondió con error ${r.statusCode}.',
          deMadeline: true, esError: true));
      return;
    }

    final mensaje = Mensaje(datos['response']?.toString() ?? '', deMadeline: true);
    _agregar(mensaje);

    if (datos['audio_path'] != null) {
      final url = '${_conexion.base}${datos['audio_path']}?t=${DateTime.now().millisecondsSinceEpoch}';
      _reproductor.play(UrlSource(url)).catchError((_) {});
    }
    if (datos['image_path'] != null) {
      // El servidor borra la captura apenas se descarga: se baja una
      // vez y se usa para mostrar y guardar.
      _bajarCaptura(datos['image_path'].toString(), mensaje);
    }
  }

  Future<void> _bajarCaptura(String ruta, Mensaje mensaje, {VoidCallback? alLlegar}) async {
    try {
      final r = await _conexion.descargar(ruta);
      if (r.statusCode != 200) return _avisar('La captura ya no está disponible en el PC.');
      setState(() => mensaje.imagen = r.bodyBytes);
      alLlegar?.call();

      if (!await Gal.hasAccess(toAlbum: true)) await Gal.requestAccess(toAlbum: true);
      final ahora = DateTime.now().toIso8601String().replaceAll(RegExp(r'[:.]'), '-');
      await Gal.putImageBytes(r.bodyBytes, album: 'Madeline', name: 'madeline_$ahora');
      _avisar('Captura guardada en la galería (álbum Madeline).');
    } catch (e) {
      _avisar('No pude guardar la captura.');
    }
  }

  /// Una captura pedida desde el deck: se muestra en el chat.
  void _capturaDesdeDeck(String ruta) {
    final mensaje = Mensaje('📸 Captura desde el deck', deMadeline: true);
    setState(() => _pestana = 0);
    _bajarCaptura(ruta, mensaje, alLlegar: () => _agregar(mensaje));
  }

  void _avisar(String texto) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(texto)));
  }

  void _verImagen(Mensaje m) {
    showDialog(
      context: context,
      builder: (_) => Dialog.fullscreen(
        backgroundColor: Colors.black,
        child: Stack(children: [
          Center(child: InteractiveViewer(child: Image.memory(m.imagen!))),
          SafeArea(
            child: IconButton(
              icon: const Icon(Icons.close, color: Colors.white),
              onPressed: () => Navigator.pop(context),
            ),
          ),
        ]),
      ),
    );
  }

  // ------------------------------------------------------------
  // Hojas: sensores y ajustes
  // ------------------------------------------------------------

  void _abrirSensores() {
    if (_conexion.estado != EstadoConexion.conectada) return _avisar('Primero tengo que encontrar tu PC.');
    showModalBottomSheet(context: context, builder: (_) => PanelSensores(conexion: _conexion));
  }

  void _abrirAjustes() {
    final campo = TextEditingController(text: _conexion.ip ?? '');
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      builder: (contexto) => Padding(
        padding: EdgeInsets.fromLTRB(20, 0, 20, MediaQuery.of(contexto).viewInsets.bottom + 24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('Conexión con el PC', style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
            const SizedBox(height: 6),
            const Text(
              'La app busca tu PC sola en la red. Si no lo encuentra, escribe su IP '
              '(en el PC: ip addr).',
              style: TextStyle(color: Colores.textoSuave),
            ),
            const SizedBox(height: 16),
            TextField(
              controller: campo,
              keyboardType: const TextInputType.numberWithOptions(decimal: true),
              decoration: InputDecoration(
                labelText: 'IP del PC',
                hintText: '192.168.1.4',
                filled: true,
                fillColor: Colores.superficieAlta,
                border: OutlineInputBorder(borderRadius: BorderRadius.circular(14), borderSide: BorderSide.none),
              ),
            ),
            const SizedBox(height: 16),
            Row(
              children: [
                Expanded(
                  child: OutlinedButton.icon(
                    icon: const Icon(Icons.travel_explore),
                    label: const Text('Buscar sola'),
                    onPressed: () {
                      Navigator.pop(contexto);
                      _conectar(forzar: true);
                    },
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: FilledButton.icon(
                    icon: const Icon(Icons.check),
                    label: const Text('Usar esta IP'),
                    onPressed: () async {
                      Navigator.pop(contexto);
                      setState(() => _conexion.estado = EstadoConexion.buscando);
                      final ok = await _conexion.usarIp(campo.text);
                      if (!ok) _conexion.estado = EstadoConexion.sinConexion;
                      if (mounted) setState(() {});
                      _avisar(ok ? 'Conectada a ${campo.text.trim()}.' : 'Madeline no responde en esa IP.');
                    },
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  // ------------------------------------------------------------
  // Interfaz
  // ------------------------------------------------------------

  Widget _estado() {
    final (color, texto) = switch (_conexion.estado) {
      EstadoConexion.conectada => (Colores.ok, 'Conectada · ${_conexion.ip}'),
      EstadoConexion.buscando => (Colores.aviso, 'Buscando tu PC…'),
      EstadoConexion.sinConexion => (Colores.pelo, 'Sin conexión · toca para reintentar'),
    };
    return GestureDetector(
      onTap: _conexion.estado == EstadoConexion.sinConexion ? () => _conectar(forzar: true) : null,
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(width: 8, height: 8, decoration: BoxDecoration(color: color, shape: BoxShape.circle)),
          const SizedBox(width: 6),
          Text(texto, style: const TextStyle(color: Colores.textoSuave, fontSize: 12.5)),
        ],
      ),
    );
  }

  Widget _cabecera() {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 8, 8, 8),
      child: Row(
        children: [
          Container(
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              boxShadow: [BoxShadow(color: Colores.fresa.withValues(alpha: 0.35), blurRadius: 16)],
            ),
            child: const CircleAvatar(radius: 22, backgroundImage: AssetImage('assets/logo.png')),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text('Madeline', style: TextStyle(fontSize: 22, fontWeight: FontWeight.w800, letterSpacing: 0.5)),
                const SizedBox(height: 2),
                _estado(),
              ],
            ),
          ),
          IconButton(
            tooltip: 'Estado del PC',
            icon: const Icon(Icons.monitor_heart_outlined, color: Colores.textoSuave),
            onPressed: _abrirSensores,
          ),
          IconButton(
            tooltip: 'Conexión',
            icon: const Icon(Icons.tune, color: Colores.textoSuave),
            onPressed: _abrirAjustes,
          ),
        ],
      ),
    );
  }

  Widget _botonMicrofono() {
    // Rojo como el pelo de Madeline; azul mientras graba, como cuando
    // gasta el dash en Celeste.
    final color = _grabando ? Colores.dash : Colores.pelo;
    return GestureDetector(
      onTapDown: (_) {
        _dedoAbajo = true;
        _empezarGrabacion();
      },
      onTapUp: (_) {
        _dedoAbajo = false;
        _terminarGrabacion();
      },
      onTapCancel: () {
        _dedoAbajo = false;
        _terminarGrabacion();
      },
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 180),
        width: _grabando ? 68 : 56,
        height: _grabando ? 68 : 56,
        decoration: BoxDecoration(
          color: color,
          shape: BoxShape.circle,
          boxShadow: [
            BoxShadow(
                color: color.withValues(alpha: 0.55), blurRadius: _grabando ? 26 : 14, spreadRadius: _grabando ? 4 : 0)
          ],
        ),
        child: Icon(_grabando ? Icons.graphic_eq : Icons.mic, color: Colors.white, size: _grabando ? 32 : 26),
      ),
    );
  }

  Widget _barraInferior() {
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 4, 12, 12),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          AnimatedOpacity(
            opacity: _grabando ? 1 : 0,
            duration: const Duration(milliseconds: 150),
            child: const Padding(
              padding: EdgeInsets.only(bottom: 8),
              child: Text('Te escucho… suelta para enviar',
                  style: TextStyle(color: Colores.dash, fontWeight: FontWeight.w600)),
            ),
          ),
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Expanded(
                child: TextField(
                  controller: _texto,
                  minLines: 1,
                  maxLines: 4,
                  textCapitalization: TextCapitalization.sentences,
                  textInputAction: TextInputAction.send,
                  style: const TextStyle(color: Colores.texto),
                  onSubmitted: (_) => _enviarTexto(),
                  decoration: InputDecoration(
                    hintText: 'Escríbele a Madeline…',
                    hintStyle: const TextStyle(color: Colores.textoSuave),
                    filled: true,
                    fillColor: Colores.superficie.withValues(alpha: 0.95),
                    contentPadding: const EdgeInsets.symmetric(horizontal: 18, vertical: 14),
                    border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(28), borderSide: const BorderSide(color: Colores.borde)),
                    enabledBorder: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(28), borderSide: const BorderSide(color: Colores.borde)),
                    focusedBorder: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(28), borderSide: const BorderSide(color: Colores.fresa)),
                  ),
                ),
              ),
              const SizedBox(width: 10),
              SizedBox(
                width: 68,
                height: 68,
                child: Center(
                  child: _hayTexto
                      ? GestureDetector(
                          onTap: _enviarTexto,
                          child: Container(
                            width: 56,
                            height: 56,
                            decoration: const BoxDecoration(color: Colores.pelo, shape: BoxShape.circle),
                            child: const Icon(Icons.arrow_upward_rounded, color: Colors.white, size: 28),
                          ),
                        )
                      : _botonMicrofono(),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      resizeToAvoidBottomInset: true,
      body: FondoMontana(
        child: SafeArea(
          child: Column(
            children: [
              _cabecera(),
              Expanded(
                child: IndexedStack(
                  index: _pestana,
                  children: [
                    _chat(),
                    PantallaDeck(conexion: _conexion, alLlegarCaptura: _capturaDesdeDeck),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _pestana,
        height: 62,
        backgroundColor: Colores.montanaFrente,
        indicatorColor: Colores.pelo.withValues(alpha: 0.25),
        labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
        onDestinationSelected: (i) {
          FocusScope.of(context).unfocus();
          setState(() => _pestana = i);
        },
        destinations: const [
          NavigationDestination(
              icon: Icon(Icons.chat_bubble_outline_rounded),
              selectedIcon: Icon(Icons.chat_bubble_rounded),
              label: 'Chat'),
          NavigationDestination(
              icon: Icon(Icons.grid_view_outlined), selectedIcon: Icon(Icons.grid_view_rounded), label: 'Deck'),
        ],
      ),
    );
  }

  Widget _chat() {
    return Column(
      children: [
        Expanded(
          child: GestureDetector(
            onTap: () => FocusScope.of(context).unfocus(),
            child: ListView.builder(
              controller: _scroll,
              padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
              itemCount: _mensajes.length + (_pensando ? 1 : 0),
              itemBuilder: (_, i) {
                if (i == _mensajes.length) return const Escribiendo();
                final m = _mensajes[i];
                return Burbuja(mensaje: m, alTocarImagen: m.imagen == null ? null : () => _verImagen(m));
              },
            ),
          ),
        ),
        _barraInferior(),
      ],
    );
  }
}
