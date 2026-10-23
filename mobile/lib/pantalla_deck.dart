import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'conexion.dart';
import 'tema.dart';

/// Botones como un Stream Deck: cada tecla ejecuta una acción directa
/// en el PC (sin pasar por Groq). Las carpetas se abren en el mismo
/// tablero, con la tecla "volver" arriba a la izquierda.
class PantallaDeck extends StatefulWidget {
  const PantallaDeck({super.key, required this.conexion, required this.alLlegarCaptura});

  final Conexion conexion;

  /// El deck no muestra imágenes: la captura se va al chat.
  final void Function(String ruta) alLlegarCaptura;

  @override
  State<PantallaDeck> createState() => _PantallaDeckState();
}

enum Carpeta { inicio, apps, multimedia, escritorios, sistema, bluetooth }

/// Una tecla del deck.
class Tecla {
  const Tecla(this.etiqueta, this.icono, this.color, this.tool,
      {this.args = const {}, this.confirmar, this.activa = false, this.imagen});

  final String etiqueta;
  final IconData icono;
  final Color color;
  final String tool;
  final Map<String, dynamic> args;

  /// Si no es null, pide confirmación con este texto antes de ejecutar.
  final String? confirmar;

  /// Resaltada (el escritorio en el que estás).
  final bool activa;

  /// Logo de la app (reemplaza al ícono).
  final String? imagen;
}

class _PantallaDeckState extends State<PantallaDeck> {
  Carpeta _carpeta = Carpeta.inicio;

  List<Map<String, dynamic>>? _apps;
  bool _cargandoApps = false;
  String _filtro = '';

  Map<String, dynamic>? _estado;

  Conexion get _conexion => widget.conexion;

  // ------------------------------------------------------------
  // Datos
  // ------------------------------------------------------------

  void _abrir(Carpeta carpeta) {
    HapticFeedback.selectionClick();
    setState(() {
      _carpeta = carpeta;
      _filtro = '';
    });
    if (carpeta == Carpeta.apps && _apps == null) _cargarApps();
    if (carpeta != Carpeta.apps && carpeta != Carpeta.multimedia) _cargarEstado();
  }

  Future<void> _cargarApps() async {
    setState(() => _cargandoApps = true);
    final apps = await _conexion.deckApps();
    if (!mounted) return;
    setState(() {
      _apps = apps;
      _cargandoApps = false;
    });
  }

  Future<void> _cargarEstado() async {
    final estado = await _conexion.deckEstado();
    if (mounted) setState(() => _estado = estado);
  }

  /// Ejecuta la tecla. Devuelve true si el PC dijo que salió bien.
  Future<bool> _ejecutar(Tecla t) async {
    if (t.confirmar != null && !await _confirmar(t.confirmar!)) return false;
    try {
      final r = await _conexion.deckAccion(t.tool, t.args);
      final datos = jsonDecode(utf8.decode(r.bodyBytes)) as Map<String, dynamic>;
      if (r.statusCode != 200) {
        _avisar(datos['detail']?.toString() ?? 'El PC respondió con error ${r.statusCode}.');
        return false;
      }
      final texto = datos['response']?.toString() ?? '';
      if (datos['image_path'] != null) widget.alLlegarCaptura(datos['image_path'].toString());
      if (texto.isNotEmpty) _avisar(texto);
      if (t.tool == 'cambiar_escritorio' || t.tool == 'mostrar_avatar') _cargarEstado();
      return !RegExp(r'^(error|no pude|pucha|falló)', caseSensitive: false).hasMatch(texto);
    } catch (_) {
      _avisar('No encuentro tu PC en esta red.');
      return false;
    }
  }

  Future<bool> _confirmar(String pregunta) async {
    final si = await showDialog<bool>(
      context: context,
      builder: (contexto) => AlertDialog(
        backgroundColor: Colores.superficie,
        content: Text(pregunta, style: const TextStyle(fontSize: 16)),
        actions: [
          TextButton(onPressed: () => Navigator.pop(contexto, false), child: const Text('Cancelar')),
          FilledButton(onPressed: () => Navigator.pop(contexto, true), child: const Text('Sí')),
        ],
      ),
    );
    return si ?? false;
  }

  void _avisar(String texto) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(texto), duration: const Duration(seconds: 2)));
  }

  /// Mantener presionada una app: abrirla en un escritorio o cerrarla.
  void _opcionesApp(Map<String, dynamic> app) {
    final id = app['id'] as String;
    final nombre = app['nombre'] as String;
    showModalBottomSheet(
      context: context,
      builder: (contexto) => Padding(
        padding: const EdgeInsets.fromLTRB(20, 0, 20, 24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(nombre, style: const TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
            const SizedBox(height: 14),
            const Text('Abrir en el escritorio', style: TextStyle(color: Colores.textoSuave)),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                for (var n = 1; n <= 9; n++)
                  OutlinedButton(
                    onPressed: () {
                      Navigator.pop(contexto);
                      _ejecutar(Tecla(nombre, Icons.apps, Colores.fresa, 'abrir_aplicacion',
                          args: {'app_name': id, 'escritorio': n}));
                    },
                    child: Text('$n'),
                  ),
              ],
            ),
            const SizedBox(height: 16),
            SizedBox(
              width: double.infinity,
              child: FilledButton.icon(
                icon: const Icon(Icons.close),
                label: Text('Cerrar $nombre'),
                onPressed: () {
                  Navigator.pop(contexto);
                  _ejecutar(Tecla(nombre, Icons.close, Colores.peligro, 'cerrar_aplicacion',
                      args: {'app_name': id}, confirmar: '¿Cierro $nombre?'));
                },
              ),
            ),
          ],
        ),
      ),
    );
  }

  // ------------------------------------------------------------
  // Teclas de cada carpeta
  // ------------------------------------------------------------

  static const _carpetas = [
    (Carpeta.apps, 'Apps', Icons.apps_rounded, Colores.fresa),
    (Carpeta.multimedia, 'Multimedia', Icons.music_note_rounded, Colores.pelo),
    (Carpeta.escritorios, 'Escritorios', Icons.space_dashboard_rounded, Colores.dash),
    (Carpeta.sistema, 'Sistema', Icons.memory_rounded, Colores.aviso),
    (Carpeta.bluetooth, 'Bluetooth', Icons.bluetooth_rounded, Color(0xFF8B7BFF)),
  ];

  List<Tecla> _teclasMultimedia() {
    const c = Colores.pelo;
    return const [
      Tecla('Radio', Icons.radio_rounded, c, 'abrir_radio_musica'),
      Tecla('Anterior', Icons.skip_previous_rounded, c, 'control_multimedia', args: {'accion': 'anterior'}),
      Tecla('Play', Icons.play_arrow_rounded, c, 'control_multimedia', args: {'accion': 'reproducir'}),
      Tecla('Pausa', Icons.pause_rounded, c, 'control_multimedia', args: {'accion': 'pausar'}),
      Tecla('Siguiente', Icons.skip_next_rounded, c, 'control_multimedia', args: {'accion': 'siguiente'}),
      Tecla('Desde el inicio', Icons.replay_rounded, c, 'control_multimedia', args: {'accion': 'reiniciar'}),
      Tecla('¿Qué suena?', Icons.lyrics_rounded, c, 'info_cancion'),
      Tecla('Bajar vol.', Icons.volume_down_rounded, Colores.dash, 'ajustar_volumen',
          args: {'accion': 'bajar', 'porcentaje': 10}),
      Tecla('Subir vol.', Icons.volume_up_rounded, Colores.dash, 'ajustar_volumen',
          args: {'accion': 'subir', 'porcentaje': 10}),
      Tecla('Silenciar', Icons.volume_off_rounded, Colores.dash, 'ajustar_volumen', args: {'accion': 'mutear'}),
    ];
  }

  List<Tecla> _teclasEscritorios() {
    final activo = _estado?['activo'];
    return [
      for (var n = 1; n <= 9; n++)
        Tecla('Escritorio $n', Icons.desktop_windows_rounded, Colores.dash, 'cambiar_escritorio',
            args: {'escritorio': n}, activa: activo == n),
    ];
  }

  List<Tecla> _teclasSistema() {
    const c = Colores.aviso;
    // La monita que pasea por la pantalla del PC: prendida se ve resaltada.
    final avatar = _estado?['avatar'] == true;
    return [
      Tecla(avatar ? 'Esconder avatar' : 'Mostrar avatar', Icons.directions_walk_rounded, Colores.pelo,
          'mostrar_avatar', args: {'accion': avatar ? 'esconder' : 'mostrar'}, activa: avatar),
      const Tecla('Captura', Icons.screenshot_monitor_rounded, c, 'tomar_captura'),
      const Tecla('Estado del PC', Icons.monitor_heart_rounded, c, 'estado_sistema'),
      const Tecla('Clima', Icons.wb_cloudy_rounded, c, 'reporte_clima', args: {'dia': 'ahora'}),
      const Tecla('Clima mañana', Icons.umbrella_rounded, c, 'reporte_clima', args: {'dia': 'mañana'}),
      const Tecla('Actualizaciones', Icons.system_update_alt_rounded, c, 'verificar_actualizaciones'),
      const Tecla('Cerrar todo', Icons.cleaning_services_rounded, Colores.fresa, 'cerrar_todo',
          confirmar: '¿Cierro el navegador y los juegos?'),
      const Tecla('Reiniciar PC', Icons.restart_alt_rounded, Colores.peligro, 'control_energia',
          args: {'accion': 'reiniciar'}, confirmar: '¿Reinicio el PC?'),
      const Tecla('Apagar PC', Icons.power_settings_new_rounded, Colores.peligro, 'control_energia',
          args: {'accion': 'apagar'}, confirmar: '¿Apago el PC?'),
    ];
  }

  List<Tecla> _teclasBluetooth() {
    const c = Color(0xFF8B7BFF);
    final dispositivos = (_estado?['bluetooth'] as List?)?.cast<String>() ?? const <String>[];
    return [
      const Tecla('Encender', Icons.bluetooth_rounded, c, 'controlar_bluetooth', args: {'accion': 'encender'}),
      const Tecla('Apagar', Icons.bluetooth_disabled_rounded, c, 'controlar_bluetooth', args: {'accion': 'apagar'}),
      for (final d in dispositivos) ...[
        Tecla('Conectar $d', Icons.bluetooth_connected_rounded, Colores.ok, 'conectar_bluetooth',
            args: {'dispositivo': d}),
        Tecla('Desconectar $d', Icons.link_off_rounded, Colores.textoSuave, 'desconectar_bluetooth',
            args: {'dispositivo': d}),
      ],
    ];
  }

  List<Tecla> _teclasApps() {
    final filtro = _filtro.toLowerCase();
    return [
      for (final app in _apps ?? const <Map<String, dynamic>>[])
        if (filtro.isEmpty || (app['nombre'] as String).toLowerCase().contains(filtro))
          Tecla(app['nombre'] as String, Icons.apps_rounded, Colores.fresa, 'abrir_aplicacion',
              args: {'app_name': app['id']}, imagen: _conexion.iconoApp(app['id'] as String)),
    ];
  }

  // ------------------------------------------------------------
  // Interfaz
  // ------------------------------------------------------------

  String get _titulo => switch (_carpeta) {
        Carpeta.inicio => 'Deck',
        Carpeta.apps => 'Apps',
        Carpeta.multimedia => 'Multimedia',
        Carpeta.escritorios => 'Escritorios',
        Carpeta.sistema => 'Sistema',
        Carpeta.bluetooth => 'Bluetooth',
      };

  Widget _tablero(List<Widget> teclas, {int columnas = 3}) {
    return GridView.count(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
      crossAxisCount: columnas,
      mainAxisSpacing: 12,
      crossAxisSpacing: 12,
      children: teclas,
    );
  }

  Widget _contenido() {
    final volver = TeclaDeck(
      etiqueta: 'Volver',
      icono: Icons.arrow_back_rounded,
      color: Colores.textoSuave,
      alTocar: () async {
        _abrir(Carpeta.inicio);
        return true;
      },
    );

    Widget teclas(List<Tecla> lista, {int columnas = 3}) => _tablero([
          volver,
          for (final t in lista)
            TeclaDeck(
              etiqueta: t.etiqueta,
              icono: t.icono,
              color: t.color,
              activa: t.activa,
              imagen: t.imagen,
              cabeceras: _conexion.cabeceras,
              alTocar: () => _ejecutar(t),
            ),
        ], columnas: columnas);

    switch (_carpeta) {
      case Carpeta.inicio:
        return _tablero([
          for (final (carpeta, etiqueta, icono, color) in _carpetas)
            TeclaDeck(
              etiqueta: etiqueta,
              icono: icono,
              color: color,
              grande: true,
              alTocar: () async {
                _abrir(carpeta);
                return true;
              },
            ),
        ], columnas: 2);
      case Carpeta.multimedia:
        return teclas(_teclasMultimedia());
      case Carpeta.escritorios:
        return teclas(_teclasEscritorios());
      case Carpeta.sistema:
        return teclas(_teclasSistema());
      case Carpeta.bluetooth:
        return teclas(_teclasBluetooth());
      case Carpeta.apps:
        if (_cargandoApps) return const Center(child: CircularProgressIndicator(color: Colores.fresa));
        if (_apps == null) {
          return Center(
            child: OutlinedButton.icon(
              icon: const Icon(Icons.refresh),
              label: const Text('No pude traer tus apps · reintentar'),
              onPressed: _cargarApps,
            ),
          );
        }
        final lista = _teclasApps();
        return Column(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 4, 16, 4),
              child: TextField(
                onChanged: (v) => setState(() => _filtro = v),
                style: const TextStyle(color: Colores.texto),
                decoration: InputDecoration(
                  hintText: 'Buscar app…',
                  hintStyle: const TextStyle(color: Colores.textoSuave),
                  prefixIcon: const Icon(Icons.search, color: Colores.textoSuave),
                  isDense: true,
                  filled: true,
                  fillColor: Colores.superficie.withValues(alpha: 0.95),
                  border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(16), borderSide: const BorderSide(color: Colores.borde)),
                  enabledBorder: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(16), borderSide: const BorderSide(color: Colores.borde)),
                ),
              ),
            ),
            Expanded(
              child: _tablero([
                volver,
                for (final t in lista)
                  TeclaDeck(
                    etiqueta: t.etiqueta,
                    icono: t.icono,
                    color: t.color,
                    imagen: t.imagen,
                    cabeceras: _conexion.cabeceras,
                    alTocar: () => _ejecutar(t),
                    alMantener: () => _opcionesApp(_apps!.firstWhere((a) => a['id'] == t.args['app_name'])),
                  ),
              ], columnas: 4),
            ),
          ],
        );
    }
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      // El botón atrás del celular sale de la carpeta, no de la app.
      canPop: _carpeta == Carpeta.inicio,
      onPopInvokedWithResult: (salio, _) {
        if (!salio) _abrir(Carpeta.inicio);
      },
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 4, 20, 8),
            child: Row(
              children: [
                Text(_titulo, style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700)),
                const Spacer(),
                if (_carpeta == Carpeta.apps)
                  const Text('Mantén presionada para más', style: TextStyle(color: Colores.textoSuave, fontSize: 12)),
              ],
            ),
          ),
          Expanded(
            child: AnimatedSwitcher(
              duration: const Duration(milliseconds: 180),
              child: KeyedSubtree(key: ValueKey(_carpeta), child: _contenido()),
            ),
          ),
        ],
      ),
    );
  }
}

/// Una tecla cuadrada: se hunde al tocarla, muestra que está
/// trabajando y destella verde o rojo según cómo le fue.
class TeclaDeck extends StatefulWidget {
  const TeclaDeck({
    super.key,
    required this.etiqueta,
    required this.icono,
    required this.color,
    required this.alTocar,
    this.alMantener,
    this.imagen,
    this.cabeceras = const {},
    this.activa = false,
    this.grande = false,
  });

  final String etiqueta;
  final IconData icono;
  final Color color;
  final Future<bool> Function() alTocar;
  final VoidCallback? alMantener;
  final String? imagen;
  final Map<String, String> cabeceras;
  final bool activa;
  final bool grande;

  @override
  State<TeclaDeck> createState() => _TeclaDeckState();
}

class _TeclaDeckState extends State<TeclaDeck> {
  bool _hundida = false;
  bool _trabajando = false;
  Color? _destello;

  Future<void> _tocar() async {
    if (_trabajando) return;
    HapticFeedback.lightImpact();
    setState(() => _trabajando = true);
    final ok = await widget.alTocar();
    if (!mounted) return;
    setState(() {
      _trabajando = false;
      _destello = ok ? Colores.ok : Colores.peligro;
    });
    await Future.delayed(const Duration(milliseconds: 450));
    if (mounted) setState(() => _destello = null);
  }

  @override
  Widget build(BuildContext context) {
    final borde = _destello ?? (widget.activa ? widget.color : Colores.borde);
    final tamanoIcono = widget.grande ? 44.0 : 30.0;

    Widget simbolo = widget.imagen == null
        ? Icon(widget.icono, color: widget.color, size: tamanoIcono)
        : Image.network(
            widget.imagen!,
            headers: widget.cabeceras,
            width: 40,
            height: 40,
            errorBuilder: (_, __, ___) => Icon(widget.icono, color: widget.color, size: tamanoIcono),
          );
    if (_trabajando) {
      simbolo = SizedBox(
        width: tamanoIcono,
        height: tamanoIcono,
        child: Padding(
          padding: const EdgeInsets.all(4),
          child: CircularProgressIndicator(strokeWidth: 2.5, color: widget.color),
        ),
      );
    }

    return GestureDetector(
      onTapDown: (_) => setState(() => _hundida = true),
      onTapUp: (_) => setState(() => _hundida = false),
      onTapCancel: () => setState(() => _hundida = false),
      onTap: _tocar,
      onLongPress: widget.alMantener == null
          ? null
          : () {
              HapticFeedback.mediumImpact();
              setState(() => _hundida = false);
              widget.alMantener!();
            },
      child: AnimatedScale(
        scale: _hundida ? 0.93 : 1,
        duration: const Duration(milliseconds: 90),
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 160),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(18),
            gradient: LinearGradient(
              begin: Alignment.topCenter,
              end: Alignment.bottomCenter,
              colors: [
                Color.lerp(Colores.superficieAlta, widget.color, widget.activa ? 0.35 : 0.08)!,
                Colores.superficie,
              ],
            ),
            border: Border.all(color: borde, width: _destello != null || widget.activa ? 2 : 1),
            boxShadow: [
              BoxShadow(
                color: (_destello ?? widget.color).withValues(alpha: _hundida || _destello != null ? 0.45 : 0.12),
                blurRadius: _hundida ? 18 : 10,
              ),
            ],
          ),
          padding: const EdgeInsets.all(6),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              simbolo,
              const SizedBox(height: 6),
              Text(
                widget.etiqueta,
                textAlign: TextAlign.center,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  fontSize: widget.grande ? 15 : 11.5,
                  fontWeight: widget.grande ? FontWeight.w700 : FontWeight.w500,
                  height: 1.1,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
