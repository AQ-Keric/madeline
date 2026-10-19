import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'conexion.dart';
import 'recados_locales.dart';
import 'tema.dart';

/// Los recados (los mismos del panel de la barra del PC) y lo que viene en
/// la agenda los próximos 7 días. Todo directo, sin pasar por Groq. Fuera
/// de casa se anota y tacha igual: queda en el celular hasta encontrar al PC.
class PantallaRecados extends StatefulWidget {
  const PantallaRecados({super.key, required this.conexion, required this.recados, required this.activa});

  final Conexion conexion;
  final RecadosLocales recados;

  /// Si la pestaña está a la vista: al volver a ella se recarga.
  final bool activa;

  @override
  State<PantallaRecados> createState() => _PantallaRecadosState();
}

const _dias = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo'];
const _meses = [
  'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
  'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre',
];

class _PantallaRecadosState extends State<PantallaRecados> {
  final _nuevo = TextEditingController();

  static const _claveAgenda = 'agenda_copia';

  List<Recado>? _recados;
  List<Map<String, dynamic>>? _eventos;
  bool _cargando = false;
  bool _enLinea = true;
  int _porSincronizar = 0;

  /// La agenda viene de la copia guardada (sin conexión con el PC).
  bool _agendaGuardada = false;

  Conexion get _conexion => widget.conexion;

  @override
  void initState() {
    super.initState();
    if (widget.activa) _cargar();
  }

  @override
  void didUpdateWidget(PantallaRecados anterior) {
    super.didUpdateWidget(anterior);
    if (widget.activa && !anterior.activa) _cargar();
  }

  @override
  void dispose() {
    _nuevo.dispose();
    super.dispose();
  }

  /// Muestra al tiro lo guardado en el celular y después intenta sincronizar.
  Future<void> _mostrarLocal() async {
    final lista = await widget.recados.lista();
    final porSincronizar = await widget.recados.pendientes();
    if (mounted) {
      setState(() {
        _recados = lista;
        _porSincronizar = porSincronizar;
      });
    }
  }

  Future<void> _cargar() async {
    if (_cargando) return;
    setState(() => _cargando = true);
    await _mostrarLocal();
    final enLinea = await widget.recados.sincronizar();
    await _mostrarLocal();

    final agenda = enLinea ? await _conexion.agenda() : null;
    final prefs = await SharedPreferences.getInstance();
    if (agenda != null && agenda['error'] == null) await prefs.setString(_claveAgenda, jsonEncode(agenda['eventos']));
    final copia = prefs.getString(_claveAgenda);
    final hoy = DateTime.now().toIso8601String().substring(0, 10);
    if (!mounted) return;
    setState(() {
      _cargando = false;
      _enLinea = enLinea;
      _agendaGuardada = agenda?['error'] != null || agenda == null;
      _eventos = agenda != null && agenda['error'] == null
          ? (agenda['eventos'] as List).cast<Map<String, dynamic>>()
          : copia == null
              ? null
              // Lo ya pasado de la copia no se muestra.
              : (jsonDecode(copia) as List)
                  .cast<Map<String, dynamic>>()
                  .where((e) => (e['fecha'] as String).compareTo(hoy) >= 0)
                  .toList();
    });
  }

  Future<void> _agregar() async {
    final texto = _nuevo.text.trim();
    if (texto.isEmpty) return;
    _nuevo.clear();
    HapticFeedback.lightImpact();
    await widget.recados.agregar(texto);
    _cargar();
  }

  Future<void> _tachar(Recado recado) async {
    HapticFeedback.selectionClick();
    setState(() => _recados?.remove(recado));
    await widget.recados.tachar(recado);
    _cargar();
  }

  // ------------------------------------------------------------
  // Interfaz
  // ------------------------------------------------------------

  Widget _titulo(String texto) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(4, 20, 4, 10),
      child: Text(texto.toUpperCase(),
          style: const TextStyle(
              color: Colores.textoSuave, fontSize: 12, fontWeight: FontWeight.w700, letterSpacing: 1.1)),
    );
  }

  Widget _tarjeta(Widget hijo) {
    return Container(
      decoration: BoxDecoration(
        color: Colores.superficie.withValues(alpha: 0.9),
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: Colores.borde),
      ),
      child: hijo,
    );
  }

  Widget _vacio(String texto) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 18, horizontal: 16),
      child: Text(texto, style: const TextStyle(color: Colores.textoSuave)),
    );
  }

  Widget _entrada() {
    return TextField(
      controller: _nuevo,
      textCapitalization: TextCapitalization.sentences,
      textInputAction: TextInputAction.done,
      onSubmitted: (_) => _agregar(),
      style: const TextStyle(color: Colores.texto),
      decoration: InputDecoration(
        hintText: 'Nuevo recado…',
        hintStyle: const TextStyle(color: Colores.textoSuave),
        filled: true,
        fillColor: Colores.superficie.withValues(alpha: 0.95),
        contentPadding: const EdgeInsets.symmetric(horizontal: 18, vertical: 14),
        suffixIcon: IconButton(icon: const Icon(Icons.add_rounded, color: Colores.fresa), onPressed: _agregar),
        border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(28), borderSide: const BorderSide(color: Colores.borde)),
        enabledBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(28), borderSide: const BorderSide(color: Colores.borde)),
        focusedBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(28), borderSide: const BorderSide(color: Colores.fresa)),
      ),
    );
  }

  /// "Sin conexión con el PC: 2 cambios se mandan al volver a casa".
  Widget _aviso() {
    if (_enLinea && _porSincronizar == 0) return const SizedBox.shrink();
    final cambios = _porSincronizar == 1 ? '1 cambio' : '$_porSincronizar cambios';
    final texto = _enLinea
        ? 'Sincronizando $cambios…'
        : _porSincronizar == 0
            ? 'Sin conexión con el PC. Lo que anotes se manda al volver a casa.'
            : 'Sin conexión con el PC: $cambios se mandan al volver a casa.';
    return Padding(
      padding: const EdgeInsets.only(top: 12),
      child: Row(
        children: [
          const Icon(Icons.cloud_off_rounded, size: 18, color: Colores.aviso),
          const SizedBox(width: 8),
          Expanded(child: Text(texto, style: const TextStyle(color: Colores.aviso, fontSize: 13))),
        ],
      ),
    );
  }

  Widget _listaRecados() {
    final lista = _recados;
    if (lista == null) return _tarjeta(_vacio('Cargando…'));
    if (lista.isEmpty) return _tarjeta(_vacio('Sin recados. Qué orden.'));
    return _tarjeta(Column(
      children: [
        for (final (i, r) in lista.indexed) ...[
          if (i > 0) const Divider(height: 1, color: Colores.borde, indent: 52),
          ListTile(
            leading: IconButton(
              tooltip: 'Tachar',
              icon: const Icon(Icons.check_box_outline_blank_rounded, color: Colores.fresa),
              onPressed: () => _tachar(r),
            ),
            title: Text(r.texto),
            trailing: r.pendiente
                ? const Tooltip(
                    message: 'Se manda al PC al volver a casa',
                    child: Icon(Icons.cloud_upload_outlined, size: 20, color: Colores.textoSuave),
                  )
                : null,
          ),
        ],
      ],
    ));
  }

  String _dia(DateTime d) {
    final hoy = DateTime.now();
    final diferencia = DateTime(d.year, d.month, d.day).difference(DateTime(hoy.year, hoy.month, hoy.day)).inDays;
    if (diferencia == 0) return 'Hoy';
    if (diferencia == 1) return 'Mañana';
    final nombre = _dias[d.weekday - 1];
    return '${nombre[0].toUpperCase()}${nombre.substring(1)} ${d.day} de ${_meses[d.month - 1]}';
  }

  Widget _agenda() {
    final eventos = _eventos;
    if (eventos == null) {
      return _tarjeta(_vacio(_cargando ? 'Cargando…' : 'No pude revisar tu calendario.'));
    }
    if (eventos.isEmpty) return _tarjeta(_vacio('Nada agendado esta semana.'));

    final porDia = <String, List<Map<String, dynamic>>>{};
    for (final e in eventos) {
      porDia.putIfAbsent(e['fecha'] as String, () => []).add(e);
    }
    return Column(
      children: [
        for (final MapEntry(key: fecha, value: delDia) in porDia.entries)
          Padding(
            padding: const EdgeInsets.only(bottom: 10),
            child: _tarjeta(Padding(
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 6),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(_dia(DateTime.parse(fecha)),
                      style: const TextStyle(color: Colores.fresa, fontWeight: FontWeight.w700)),
                  const SizedBox(height: 6),
                  for (final e in delDia)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 8),
                      child: Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          SizedBox(
                            width: 80,
                            child: Text((e['hora'] as String).isEmpty ? 'Todo el día' : e['hora'] as String,
                                style: const TextStyle(color: Colores.textoSuave, fontSize: 13)),
                          ),
                          Expanded(child: Text(e['titulo']?.toString() ?? '')),
                        ],
                      ),
                    ),
                ],
              ),
            )),
          ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      color: Colores.fresa,
      backgroundColor: Colores.superficie,
      onRefresh: _cargar,
      child: GestureDetector(
        onTap: () => FocusScope.of(context).unfocus(),
        child: ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.fromLTRB(12, 4, 12, 24),
          children: [
            _titulo('Recados'),
            _entrada(),
            _aviso(),
            const SizedBox(height: 10),
            _listaRecados(),
            _titulo(_agendaGuardada && _eventos != null ? 'Próximos días · lo último que vi' : 'Próximos 7 días'),
            _agenda(),
          ],
        ),
      ),
    );
  }
}
